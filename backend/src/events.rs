//! Swarm events: persisted to Postgres, fanned out through Redis pub/sub, streamed to browsers over WebSocket.

use axum::{
    extract::{ws::{Message, WebSocket, WebSocketUpgrade}, State},
    response::IntoResponse,
};
use chrono::{DateTime, Utc};
use futures_util::{SinkExt, StreamExt};
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::sync::broadcast;
use uuid::Uuid;

use crate::SharedState;

const CHANNEL: &str = "godview.events";

#[derive(Clone, Debug, Serialize, Deserialize, sqlx::FromRow)]
pub struct SwarmEvent {
    pub id: Uuid,
    pub project_id: Uuid,
    pub task_id: Option<Uuid>,
    #[serde(default)]
    pub run_id: Option<Uuid>,
    pub kind: String,
    pub message: String,
    pub from_agent_id: Option<String>,
    pub to_agent_id: Option<String>,
    #[serde(default)]
    pub data: Option<Value>,
    pub created_at: DateTime<Utc>,
}

/// Builder-style constructor used throughout the harness.
pub struct EventDraft {
    event: SwarmEvent,
}

impl EventDraft {
    pub fn new(project_id: Uuid, task_id: Uuid, run_id: Uuid, kind: &str, message: impl Into<String>) -> Self {
        Self {
            event: SwarmEvent {
                id: Uuid::new_v4(),
                project_id,
                task_id: Some(task_id),
                run_id: Some(run_id),
                kind: kind.into(),
                message: message.into(),
                from_agent_id: None,
                to_agent_id: None,
                data: None,
                created_at: Utc::now(),
            },
        }
    }

    pub fn from(mut self, agent: &str) -> Self {
        self.event.from_agent_id = Some(agent.into());
        self
    }

    pub fn to(mut self, agent: &str) -> Self {
        self.event.to_agent_id = Some(agent.into());
        self
    }

    pub fn data(mut self, data: Value) -> Self {
        self.event.data = Some(data);
        self
    }

    pub async fn publish(self, state: &SharedState) {
        if let Err(error) = publish(state, self.event).await {
            tracing::warn!("event publish failed: {error}");
        }
    }
}

pub async fn publish(state: &SharedState, event: SwarmEvent) -> Result<(), String> {
    sqlx::query("INSERT INTO swarm_events (id, project_id, task_id, run_id, kind, message, from_agent_id, to_agent_id, data, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)")
        .bind(event.id)
        .bind(event.project_id)
        .bind(event.task_id)
        .bind(event.run_id)
        .bind(&event.kind)
        .bind(&event.message)
        .bind(&event.from_agent_id)
        .bind(&event.to_agent_id)
        .bind(&event.data)
        .bind(event.created_at)
        .execute(&state.database)
        .await
        .map_err(|error| error.to_string())?;
    let payload = serde_json::to_string(&event).map_err(|error| error.to_string())?;
    match state.redis.get_multiplexed_async_connection().await {
        Ok(mut connection) => {
            let _: Result<i64, _> = connection.publish(CHANNEL, payload).await;
        }
        // Redis is the fan-out path for multiple API instances; a single instance can still serve its own sockets.
        Err(_) => {
            let _ = state.event_tx.send(event);
        }
    }
    Ok(())
}

pub async fn relay_redis_events(redis: redis::Client, event_tx: broadcast::Sender<SwarmEvent>) {
    loop {
        if let Ok(mut subscriber) = redis.get_async_pubsub().await {
            if subscriber.subscribe(CHANNEL).await.is_ok() {
                let mut messages = subscriber.on_message();
                while let Some(message) = messages.next().await {
                    if let Ok(payload) = message.get_payload::<String>() {
                        if let Ok(event) = serde_json::from_str::<SwarmEvent>(&payload) {
                            let _ = event_tx.send(event);
                        }
                    }
                }
            }
        }
        tracing::warn!("redis relay disconnected; retrying in 2s");
        tokio::time::sleep(std::time::Duration::from_secs(2)).await;
    }
}

pub async fn websocket_handler(ws: WebSocketUpgrade, State(state): State<SharedState>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| websocket_session(socket, state))
}

async fn websocket_session(socket: WebSocket, state: SharedState) {
    let (mut sender, mut receiver) = socket.split();
    let mut event_rx = state.event_tx.subscribe();
    let send_events = tokio::spawn(async move {
        loop {
            match event_rx.recv().await {
                Ok(event) => {
                    let Ok(text) = serde_json::to_string(&event) else { continue };
                    if sender.send(Message::Text(text)).await.is_err() {
                        break;
                    }
                }
                Err(broadcast::error::RecvError::Lagged(_)) => continue,
                Err(_) => break,
            }
        }
    });
    while let Some(Ok(message)) = receiver.next().await {
        if matches!(message, Message::Close(_)) {
            break;
        }
    }
    send_events.abort();
}
