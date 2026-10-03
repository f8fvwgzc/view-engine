use std::{collections::HashMap, env, fs, sync::Arc, time::Duration};

use axum::{
    extract::{
        ws::{Message, WebSocket, WebSocketUpgrade},
        Path, State,
    },
    http::{Method, StatusCode},
    response::IntoResponse,
    routing::{get, patch, post},
    Json, Router,
};
use chrono::{DateTime, Utc};
use futures_util::{sink::SinkExt, stream::StreamExt};
use redis::AsyncCommands;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use sqlx::{postgres::PgPoolOptions, PgPool};
use tokio::sync::{broadcast, Mutex};
use tokio_tungstenite::{
    connect_async,
    tungstenite::{
        client::IntoClientRequest,
        http::{header::AUTHORIZATION, HeaderValue},
        Message as AppServerMessage,
    },
};
use tower_http::{
    cors::{Any, CorsLayer},
    trace::TraceLayer,
};
use uuid::Uuid;

type SharedState = Arc<AppState>;
type ApiResult<T> = Result<T, (StatusCode, String)>;

struct AppState {
    database: PgPool,
    redis: redis::Client,
    event_tx: broadcast::Sender<SwarmEvent>,
    run_locks: Mutex<HashMap<Uuid, bool>>,
    codex_app_server_url: Option<String>,
    codex_app_server_token: Option<String>,
    codex_workspace: String,
    agent_provider: String,
    ollama_url: Option<String>,
    ollama_model: String,
}

#[derive(Clone, Serialize, sqlx::FromRow)]
struct Agent {
    id: String,
    name: String,
    role: String,
    description: String,
    status: String,
    active_task_id: Option<Uuid>,
    x: f32,
    y: f32,
}

#[derive(Clone, Serialize, sqlx::FromRow)]
struct Task {
    id: Uuid,
    title: String,
    description: String,
    assigned_agent_ids: Vec<String>,
    status: String,
    result: Option<String>,
    completed_at: Option<DateTime<Utc>>,
    run_started_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
}

#[derive(Clone, sqlx::FromRow)]
struct TaskRow {
    id: Uuid,
    project_id: Uuid,
    title: String,
    description: String,
    assigned_agent_ids: Vec<String>,
    status: String,
    result: Option<String>,
    completed_at: Option<DateTime<Utc>>,
    run_started_at: Option<DateTime<Utc>>,
    created_at: DateTime<Utc>,
}

#[derive(Clone, Serialize)]
struct Project {
    id: Uuid,
    name: String,
    description: String,
    created_at: DateTime<Utc>,
    tasks: Vec<Task>,
}

#[derive(sqlx::FromRow)]
struct ProjectRow {
    id: Uuid,
    name: String,
    description: String,
    created_at: DateTime<Utc>,
}

#[derive(Clone, Serialize, Deserialize, sqlx::FromRow)]
struct SwarmEvent {
    id: Uuid,
    project_id: Uuid,
    task_id: Option<Uuid>,
    kind: String,
    message: String,
    from_agent_id: Option<String>,
    to_agent_id: Option<String>,
    created_at: DateTime<Utc>,
}

#[derive(Deserialize)]
struct CreateProject {
    name: String,
    description: String,
}

#[derive(Deserialize)]
struct CreateTask {
    title: String,
    description: String,
    assigned_agent_ids: Vec<String>,
}

#[derive(Deserialize)]
struct UpdateTask {
    title: Option<String>,
    description: Option<String>,
    assigned_agent_ids: Option<Vec<String>>,
    status: Option<String>,
}

#[derive(Serialize)]
struct DashboardSnapshot {
    projects: Vec<Project>,
    agents: Vec<Agent>,
    events: Vec<SwarmEvent>,
}

#[tokio::main]
async fn main() {
    let database_url = env::var("DATABASE_URL")
        .unwrap_or_else(|_| "postgres://godview:godview@localhost:5432/godview".into());
    let redis_url = env::var("REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".into());
    let codex_app_server_url = env::var("CODEX_APP_SERVER_URL").ok();
    let codex_app_server_token = env::var("CODEX_APP_SERVER_TOKEN_FILE")
        .ok()
        .and_then(|path| fs::read_to_string(path).ok())
        .map(|token| token.trim().to_string())
        .filter(|token| !token.is_empty());
    let codex_workspace = env::var("CODEX_WORKSPACE").unwrap_or_else(|_| "/workspace".into());
    let agent_provider = env::var("AGENT_PROVIDER")
        .unwrap_or_else(|_| "ollama".into())
        .to_ascii_lowercase();
    let ollama_url = env::var("OLLAMA_URL")
        .ok()
        .map(|url| url.trim_end_matches('/').to_string())
        .filter(|url| !url.is_empty());
    let ollama_model = env::var("OLLAMA_MODEL").unwrap_or_else(|_| "gemma4:E4B".into());
    let database = PgPoolOptions::new()
        .max_connections(10)
        .connect(&database_url)
        .await
        .expect("Postgres is required. Start it with docker compose up postgres redis.");
    sqlx::migrate!("./migrations")
        .run(&database)
        .await
        .expect("Postgres migration failed");
    seed_agents(&database)
        .await
        .expect("Could not seed system agents");
    recover_interrupted_runs(&database)
        .await
        .expect("Could not recover interrupted local runs");

    let redis = redis::Client::open(redis_url).expect("Invalid REDIS_URL");
    redis
        .get_multiplexed_async_connection()
        .await
        .expect("Redis is required. Start it with docker compose up postgres redis.");
    let (event_tx, _) = broadcast::channel(512);
    tokio::spawn(relay_redis_events(redis.clone(), event_tx.clone()));

    let state = Arc::new(AppState {
        database,
        redis,
        event_tx,
        run_locks: Mutex::new(HashMap::new()),
        codex_app_server_url,
        codex_app_server_token,
        codex_workspace,
        agent_provider,
        ollama_url,
        ollama_model,
    });
    let app = Router::new()
        .route("/api/health", get(|| async { Json(serde_json::json!({"status": "ok", "storage": "postgres+pgvector", "events": "redis"})) }))
        .route("/api/dashboard", get(get_dashboard))
        .route("/api/projects", get(list_projects).post(create_project))
        .route("/api/projects/:project_id", get(get_project))
        .route("/api/projects/:project_id/tasks", post(create_task))
        .route("/api/tasks/:task_id", patch(update_task))
        .route("/api/tasks/:task_id/run", post(run_task))
        .route("/api/agents", get(list_agents))
        .route("/api/events", get(list_events).post(ingest_event))
        .route("/ws", get(websocket_handler))
        .layer(CorsLayer::new().allow_origin(Any).allow_methods([Method::GET, Method::POST, Method::PATCH]).allow_headers(Any))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3001")
        .await
        .expect("Could not bind port 3001");
    println!("GodView API listening on http://localhost:3001");
    axum::serve(listener, app)
        .await
        .expect("GodView API stopped unexpectedly");
}

async fn get_dashboard(State(state): State<SharedState>) -> ApiResult<Json<DashboardSnapshot>> {
    let projects = load_projects(&state.database).await?;
    let agents = load_agents(&state.database).await?;
    let events = load_events(&state.database, 120).await?;
    Ok(Json(DashboardSnapshot {
        projects,
        agents,
        events,
    }))
}

async fn list_projects(State(state): State<SharedState>) -> ApiResult<Json<Vec<Project>>> {
    Ok(Json(load_projects(&state.database).await?))
}

async fn get_project(
    Path(project_id): Path<Uuid>,
    State(state): State<SharedState>,
) -> ApiResult<Json<Project>> {
    load_projects(&state.database)
        .await?
        .into_iter()
        .find(|project| project.id == project_id)
        .map(Json)
        .ok_or((StatusCode::NOT_FOUND, "Project not found".into()))
}

async fn create_project(
    State(state): State<SharedState>,
    Json(payload): Json<CreateProject>,
) -> ApiResult<Json<Project>> {
    let project = Project {
        id: Uuid::new_v4(),
        name: payload.name.trim().into(),
        description: payload.description.trim().into(),
        created_at: Utc::now(),
        tasks: vec![],
    };
    if project.name.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "Project name is required".into()));
    }
    sqlx::query("INSERT INTO projects (id, name, description, created_at) VALUES ($1, $2, $3, $4)")
        .bind(project.id)
        .bind(&project.name)
        .bind(&project.description)
        .bind(project.created_at)
        .execute(&state.database)
        .await
        .map_err(database_error)?;
    Ok(Json(project))
}

async fn create_task(
    Path(project_id): Path<Uuid>,
    State(state): State<SharedState>,
    Json(payload): Json<CreateTask>,
) -> ApiResult<Json<Task>> {
    let exists: Option<Uuid> = sqlx::query_scalar("SELECT id FROM projects WHERE id = $1")
        .bind(project_id)
        .fetch_optional(&state.database)
        .await
        .map_err(database_error)?;
    if exists.is_none() {
        return Err((StatusCode::NOT_FOUND, "Project not found".into()));
    }
    let task = Task {
        id: Uuid::new_v4(),
        title: payload.title.trim().into(),
        description: payload.description.trim().into(),
        assigned_agent_ids: payload.assigned_agent_ids,
        status: "ready".into(),
        result: None,
        completed_at: None,
        run_started_at: None,
        created_at: Utc::now(),
    };
    if task.title.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "Task title is required".into()));
    }
    sqlx::query("INSERT INTO tasks (id, project_id, title, description, assigned_agent_ids, status, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7)").bind(task.id).bind(project_id).bind(&task.title).bind(&task.description).bind(&task.assigned_agent_ids).bind(&task.status).bind(task.created_at).execute(&state.database).await.map_err(database_error)?;
    Ok(Json(task))
}

async fn update_task(
    Path(task_id): Path<Uuid>,
    State(state): State<SharedState>,
    Json(payload): Json<UpdateTask>,
) -> ApiResult<Json<Task>> {
    let task = sqlx::query_as::<_, Task>("UPDATE tasks SET title = COALESCE($2, title), description = COALESCE($3, description), assigned_agent_ids = COALESCE($4, assigned_agent_ids), status = COALESCE($5, status) WHERE id = $1 RETURNING id, title, description, assigned_agent_ids, status, result, completed_at, run_started_at, created_at")
        .bind(task_id).bind(payload.title).bind(payload.description).bind(payload.assigned_agent_ids).bind(payload.status).fetch_optional(&state.database).await.map_err(database_error)?;
    task.map(Json)
        .ok_or((StatusCode::NOT_FOUND, "Task not found".into()))
}

async fn list_agents(State(state): State<SharedState>) -> ApiResult<Json<Vec<Agent>>> {
    Ok(Json(load_agents(&state.database).await?))
}
async fn list_events(State(state): State<SharedState>) -> ApiResult<Json<Vec<SwarmEvent>>> {
    Ok(Json(load_events(&state.database, 200).await?))
}

async fn ingest_event(
    State(state): State<SharedState>,
    Json(event): Json<SwarmEvent>,
) -> ApiResult<Json<SwarmEvent>> {
    publish_event(&state, event.clone()).await?;
    Ok(Json(event))
}

async fn run_task(
    Path(task_id): Path<Uuid>,
    State(state): State<SharedState>,
) -> ApiResult<Json<serde_json::Value>> {
    let project_id: Option<Uuid> = sqlx::query_scalar("SELECT project_id FROM tasks WHERE id = $1")
        .bind(task_id)
        .fetch_optional(&state.database)
        .await
        .map_err(database_error)?;
    let project_id = project_id.ok_or((StatusCode::NOT_FOUND, "Task not found".into()))?;
    {
        let mut locks = state.run_locks.lock().await;
        if locks.get(&task_id).copied().unwrap_or(false) {
            return Err((StatusCode::CONFLICT, "Task is already running".into()));
        }
        locks.insert(task_id, true);
    }
    sqlx::query(
        "UPDATE tasks SET status = 'running', result = NULL, completed_at = NULL, run_started_at = NOW() WHERE id = $1",
    )
    .bind(task_id)
    .execute(&state.database)
    .await
    .map_err(database_error)?;
    let cloned_state = state.clone();
    tokio::spawn(async move {
        execute_swarm(cloned_state, project_id, task_id).await;
    });
    Ok(Json(
        serde_json::json!({"accepted": true, "task_id": task_id}),
    ))
}

async fn execute_swarm(state: SharedState, project_id: Uuid, task_id: Uuid) {
    let task = sqlx::query_as::<_, Task>("SELECT id, title, description, assigned_agent_ids, status, result, completed_at, run_started_at, created_at FROM tasks WHERE id = $1").bind(task_id).fetch_one(&state.database).await;
    let Ok(task) = task else {
        return;
    };
    let (selected_agents, decision_message) =
        if state.agent_provider == "ollama" && state.ollama_url.is_some() {
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "orchestrator_connecting",
                    "Orchestrator is consulting the local Ollama model for the worker plan.",
                    Some("orchestrator"),
                    None,
                ),
            )
            .await;
            match decide_agents_with_ollama(&state, &task).await {
                Ok((agents, rationale)) => (
                    agents,
                    format!("Ollama selected worker agents: {rationale}"),
                ),
                Err(error) => {
                    let _ = publish_event(
                        &state,
                        event(
                            project_id,
                            task_id,
                            "orchestrator_fallback",
                            &format!(
                            "Ollama planning unavailable ({error}); using local role selection."
                        ),
                            Some("orchestrator"),
                            None,
                        ),
                    )
                    .await;
                    (decide_agents(&task), "Local role selection used.".into())
                }
            }
        } else if state.agent_provider == "codex" && state.codex_app_server_url.is_some() {
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "orchestrator_connecting",
                    "Orchestrator is consulting Codex app-server for the worker plan.",
                    Some("orchestrator"),
                    None,
                ),
            )
            .await;
            match decide_agents_with_codex(&state, &task).await {
                Ok((agents, rationale)) => {
                    (agents, format!("Codex selected worker agents: {rationale}"))
                }
                Err(error) => {
                    let _ = publish_event(
                        &state,
                        event(
                            project_id,
                            task_id,
                            "orchestrator_fallback",
                            &format!(
                                "Codex planning unavailable ({error}); using local role selection."
                            ),
                            Some("orchestrator"),
                            None,
                        ),
                    )
                    .await;
                    (decide_agents(&task), "Local role selection used.".into())
                }
            }
        } else {
            (decide_agents(&task), "Local role selection used.".into())
        };
    let _ = sqlx::query("UPDATE tasks SET assigned_agent_ids = $2 WHERE id = $1")
        .bind(task_id)
        .bind(&selected_agents)
        .execute(&state.database)
        .await;
    let _ = set_agent_status(&state.database, "orchestrator", "working", Some(task_id)).await;
    let _ = publish_event(
        &state,
        event(
            project_id,
            task_id,
            "orchestrator_decision",
            &format!(
                "Analyzed task and selected {} worker agents. {decision_message}",
                selected_agents.len()
            ),
            Some("orchestrator"),
            None,
        ),
    )
    .await;
    for agent_id in &selected_agents {
        let _ = set_agent_status(&state.database, agent_id, "waiting", Some(task_id)).await;
        let _ = publish_event(
            &state,
            event(
                project_id,
                task_id,
                "orchestrator_dispatch",
                "Selected by orchestrator and added to this swarm.",
                Some("orchestrator"),
                Some(agent_id),
            ),
        )
        .await;
        tokio::time::sleep(Duration::from_millis(190)).await;
    }
    let mut workers = tokio::task::JoinSet::new();
    for agent_id in selected_agents.clone() {
        let worker_state = state.clone();
        let worker_agents = selected_agents.clone();
        workers.spawn(async move {
            if worker_state.agent_provider == "ollama" && worker_state.ollama_url.is_some() {
                run_ollama_agent(worker_state, project_id, task_id, agent_id).await
            } else if worker_state.agent_provider == "codex"
                && worker_state.codex_app_server_url.is_some()
            {
                run_codex_agent(worker_state, project_id, task_id, agent_id).await
            } else {
                run_independent_agent(worker_state, project_id, task_id, agent_id, worker_agents)
                    .await;
                Ok("Simulated worker contribution completed.".into())
            }
        });
    }
    let mut worker_outcomes = Vec::new();
    while let Some(join_result) = workers.join_next().await {
        if let Ok(Ok(outcome)) = join_result {
            worker_outcomes.push(outcome);
        }
    }
    let using_llm = (state.agent_provider == "ollama" && state.ollama_url.is_some())
        || (state.agent_provider == "codex" && state.codex_app_server_url.is_some());
    let provider_name = if state.agent_provider == "ollama" {
        "Ollama"
    } else {
        "Codex"
    };
    let completed = !using_llm || !worker_outcomes.is_empty();
    let result = if using_llm {
        format!(
            "{provider_name} swarm completed \"{}\" with {} real {provider_name} workers.\n\n{}",
            task.title,
            worker_outcomes.len(),
            worker_outcomes.join("\n\n")
        )
    } else {
        format!(
            "Completed \"{}\" with {} simulated independent agents.",
            task.title,
            selected_agents.len()
        )
    };
    let _ = sqlx::query(
        "UPDATE tasks SET status = $2, result = $3, completed_at = NOW() WHERE id = $1",
    )
    .bind(task_id)
    .bind(if completed { "complete" } else { "failed" })
    .bind(result)
    .execute(&state.database)
    .await;
    let _ = sqlx::query("UPDATE agents SET status = 'idle', active_task_id = NULL")
        .execute(&state.database)
        .await;
    let _ = publish_event(
        &state,
        event(
            project_id,
            task_id,
            if completed {
                "run_complete"
            } else {
                "run_failed"
            },
            if completed {
                "Swarm finished successfully."
            } else {
                "No AI worker completed. Check the selected provider and its local logs."
            },
            Some("orchestrator"),
            None,
        ),
    )
    .await;
    state.run_locks.lock().await.insert(task_id, false);
}

fn decide_agents(task: &Task) -> Vec<String> {
    let scope = format!("{} {}", task.title, task.description).to_lowercase();
    let words: Vec<&str> = scope
        .split(|character: char| !character.is_alphanumeric())
        .filter(|word| !word.is_empty())
        .collect();
    let needs_research = [
        "research",
        "researching",
        "find",
        "finding",
        "compare",
        "comparison",
        "library",
        "investigate",
        "analyze",
        "analysis",
        "market",
        "requirement",
    ]
    .iter()
    .any(|keyword| words.contains(keyword));
    let needs_builder = [
        "build",
        "building",
        "create",
        "creating",
        "develop",
        "developing",
        "implement",
        "implementation",
        "code",
        "ui",
        "api",
        "database",
        "fix",
    ]
    .iter()
    .any(|keyword| words.contains(keyword));

    let mut agents = vec!["planner".to_string()];
    if needs_research {
        agents.push("researcher".to_string());
    }
    if needs_builder {
        agents.push("builder".to_string());
    }
    if !needs_research && !needs_builder {
        agents.extend(["researcher".to_string(), "builder".to_string()]);
    }
    agents.push("reviewer".to_string());
    agents
}

#[derive(Deserialize)]
struct CodexWorkerPlan {
    agents: Vec<String>,
    rationale: String,
}

async fn decide_agents_with_ollama(
    state: &SharedState,
    task: &Task,
) -> Result<(Vec<String>, String), String> {
    let ollama_url = state
        .ollama_url
        .as_deref()
        .ok_or("OLLAMA_URL is not configured")?;
    let prompt = format!(
        "You are the orchestrator for a local multi-agent coding swarm. Decide which worker roles should work on this task. Available roles: planner, researcher, builder, reviewer. Return only one JSON object in this exact shape: {{\"agents\":[\"planner\"],\"rationale\":\"short reason\"}}. Do not include markdown.\n\nTask title: {}\nTask brief: {}",
        task.title, task.description
    );
    let output = run_ollama_prompt(ollama_url, &state.ollama_model, &prompt).await?;
    parse_worker_plan(&output)
}

async fn decide_agents_with_codex(
    state: &SharedState,
    task: &Task,
) -> Result<(Vec<String>, String), String> {
    let app_server_url = state
        .codex_app_server_url
        .as_deref()
        .ok_or("CODEX_APP_SERVER_URL is not configured")?;
    let prompt = format!(
        "You are the orchestrator for a local multi-agent coding swarm. Decide which worker roles should work on this task. Available roles: planner, researcher, builder, reviewer. Return only one JSON object in this exact shape: {{\"agents\":[\"planner\"],\"rationale\":\"short reason\"}}. Do not include markdown.\n\nTask title: {}\nTask brief: {}",
        task.title, task.description
    );
    let output = run_codex_turn(
        app_server_url,
        state.codex_app_server_token.as_deref(),
        &state.codex_workspace,
        &prompt,
    )
    .await?;
    parse_worker_plan(&output)
}

fn parse_worker_plan(output: &str) -> Result<(Vec<String>, String), String> {
    let start = output
        .find('{')
        .ok_or("Model did not return a worker plan")?;
    let end = output
        .rfind('}')
        .ok_or("Model did not return a complete worker plan")?;
    let plan: CodexWorkerPlan = serde_json::from_str(&output[start..=end])
        .map_err(|_| "Model returned an invalid worker plan")?;
    let allowed = ["planner", "researcher", "builder", "reviewer"];
    let mut agents = Vec::new();
    for agent in plan.agents {
        if allowed.contains(&agent.as_str()) && !agents.contains(&agent) {
            agents.push(agent);
        }
    }
    if agents.is_empty() {
        return Err("Model selected no valid worker roles".into());
    }
    Ok((agents, compact_text(&plan.rationale, 220)))
}

async fn run_ollama_agent(
    state: SharedState,
    project_id: Uuid,
    task_id: Uuid,
    agent_id: String,
) -> Result<String, String> {
    let task = sqlx::query_as::<_, Task>("SELECT id, title, description, assigned_agent_ids, status, result, completed_at, run_started_at, created_at FROM tasks WHERE id = $1")
        .bind(task_id)
        .fetch_one(&state.database)
        .await
        .map_err(|error| error.to_string())?;
    let ollama_url = state
        .ollama_url
        .as_deref()
        .ok_or("OLLAMA_URL is not configured")?;
    let _ = set_agent_status(&state.database, &agent_id, "working", Some(task_id)).await;
    let _ = publish_event(
        &state,
        event(
            project_id,
            task_id,
            "agent_ollama_started",
            "Connected to the local Ollama model and started a real worker run.",
            Some("orchestrator"),
            Some(&agent_id),
        ),
    )
    .await;
    let prompt = format!(
        "You are {} in a coordinated software swarm. Give a concise, role-specific report that the orchestrator can combine with other agents. Do not add Playwright tests or Rust test modules.\n\nTask title: {}\nTask brief: {}",
        agent_role(&agent_id), task.title, task.description
    );
    match run_ollama_prompt(ollama_url, &state.ollama_model, &prompt).await {
        Ok(output) => {
            let summary = compact_text(&output, 900);
            let _ = set_agent_status(&state.database, &agent_id, "complete", Some(task_id)).await;
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "agent_result",
                    &summary,
                    Some(&agent_id),
                    Some("orchestrator"),
                ),
            )
            .await;
            Ok(format!("{}: {}", agent_id.to_uppercase(), summary))
        }
        Err(error) => {
            let _ = set_agent_status(&state.database, &agent_id, "error", Some(task_id)).await;
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "agent_error",
                    &format!("Ollama worker failed: {error}"),
                    Some(&agent_id),
                    Some("orchestrator"),
                ),
            )
            .await;
            Err(error)
        }
    }
}

async fn run_codex_agent(
    state: SharedState,
    project_id: Uuid,
    task_id: Uuid,
    agent_id: String,
) -> Result<String, String> {
    let task = sqlx::query_as::<_, Task>("SELECT id, title, description, assigned_agent_ids, status, result, completed_at, run_started_at, created_at FROM tasks WHERE id = $1")
        .bind(task_id)
        .fetch_one(&state.database)
        .await
        .map_err(|error| error.to_string())?;
    let app_server_url = state
        .codex_app_server_url
        .as_deref()
        .ok_or("CODEX_APP_SERVER_URL is not configured")?;
    let _ = set_agent_status(&state.database, &agent_id, "working", Some(task_id)).await;
    let _ = publish_event(
        &state,
        event(
            project_id,
            task_id,
            "agent_codex_started",
            "Connected to Codex app-server and started a real worker thread.",
            Some("orchestrator"),
            Some(&agent_id),
        ),
    )
    .await;

    let prompt = format!(
        "You are {} in a coordinated software swarm. Work independently but inspect the shared workspace for useful context. Do not add Playwright tests or Rust test modules. Provide a concise final report with findings, changes made, and any handoff needed for the orchestrator.\n\nTask title: {}\nTask brief: {}",
        agent_role(&agent_id), task.title, task.description
    );
    match run_codex_turn(
        app_server_url,
        state.codex_app_server_token.as_deref(),
        &state.codex_workspace,
        &prompt,
    )
    .await
    {
        Ok(output) => {
            let summary = compact_text(&output, 900);
            let _ = set_agent_status(&state.database, &agent_id, "complete", Some(task_id)).await;
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "agent_result",
                    &summary,
                    Some(&agent_id),
                    Some("orchestrator"),
                ),
            )
            .await;
            Ok(format!("{}: {}", agent_id.to_uppercase(), summary))
        }
        Err(error) => {
            let _ = set_agent_status(&state.database, &agent_id, "error", Some(task_id)).await;
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "agent_error",
                    &format!("Codex worker failed: {error}"),
                    Some(&agent_id),
                    Some("orchestrator"),
                ),
            )
            .await;
            Err(error)
        }
    }
}

async fn run_ollama_prompt(ollama_url: &str, model: &str, prompt: &str) -> Result<String, String> {
    let response = reqwest::Client::new()
        .post(format!("{ollama_url}/api/generate"))
        .json(&serde_json::json!({
            "model": model,
            "prompt": prompt,
            "stream": false,
            "options": { "temperature": 0.2, "num_predict": 700 }
        }))
        .send()
        .await
        .map_err(|error| format!("Could not connect to Ollama: {error}"))?
        .error_for_status()
        .map_err(|error| format!("Ollama rejected the request: {error}"))?;
    let payload: Value = response
        .json()
        .await
        .map_err(|error| format!("Could not read Ollama response: {error}"))?;
    payload
        .get("response")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .filter(|output| !output.trim().is_empty())
        .ok_or("Ollama returned an empty response".into())
}

async fn run_codex_turn(
    app_server_url: &str,
    app_server_token: Option<&str>,
    workspace: &str,
    prompt: &str,
) -> Result<String, String> {
    let mut last_error = String::new();
    for attempt in 1..=2 {
        match run_codex_turn_once(app_server_url, app_server_token, workspace, prompt).await {
            Ok(output) => return Ok(output),
            Err(error) => {
                last_error = error;
                if attempt == 1 {
                    tokio::time::sleep(Duration::from_millis(350)).await;
                }
            }
        }
    }
    Err(format!(
        "Codex app-server request failed after retry: {last_error}"
    ))
}

async fn run_codex_turn_once(
    app_server_url: &str,
    app_server_token: Option<&str>,
    workspace: &str,
    prompt: &str,
) -> Result<String, String> {
    let mut request = app_server_url
        .into_client_request()
        .map_err(|error| format!("Invalid Codex app-server URL: {error}"))?;
    if let Some(token) = app_server_token {
        let header = HeaderValue::from_str(&format!("Bearer {token}"))
            .map_err(|error| format!("Invalid Codex app-server token: {error}"))?;
        request.headers_mut().insert(AUTHORIZATION, header);
    }
    let (socket, _) = connect_async(request)
        .await
        .map_err(|error| format!("Could not connect to Codex app-server: {error}"))?;
    let (mut writer, mut reader) = socket.split();
    send_app_server_message(
        &mut writer,
        serde_json::json!({
            "method": "initialize",
            "id": 1,
            "params": { "clientInfo": { "name": "godview_engine", "title": "GodView Engine", "version": "0.1.0" } }
        }),
    )
    .await?;

    tokio::time::timeout(Duration::from_secs(600), async {
        let mut output = String::new();
        let mut completed_item_text = String::new();
        while let Some(message) = reader.next().await {
            match message.map_err(|error| error.to_string())? {
                AppServerMessage::Text(text) => {
                    let message: Value = serde_json::from_str(&text)
                        .map_err(|error| format!("Invalid app-server message: {error}"))?;
                    if let Some(error) = message.get("error") {
                        return Err(format!("Codex app-server error: {error}"));
                    }
                    match message.get("id").and_then(Value::as_i64) {
                        Some(1) => {
                            send_app_server_message(&mut writer, serde_json::json!({ "method": "initialized", "params": {} })).await?;
                            send_app_server_message(&mut writer, serde_json::json!({ "method": "thread/start", "id": 2, "params": {} })).await?;
                        }
                        Some(2) => {
                            let thread_id = message.pointer("/result/thread/id").and_then(Value::as_str).ok_or("Codex app-server did not return a thread id")?;
                            send_app_server_message(&mut writer, serde_json::json!({
                                "method": "turn/start",
                                "id": 3,
                                "params": { "threadId": thread_id, "input": [{ "type": "text", "text": prompt }], "cwd": workspace }
                            })).await?;
                        }
                        _ => {}
                    }
                    match message.get("method").and_then(Value::as_str) {
                        Some("item/agentMessage/delta") => {
                            if let Some(delta) = message.pointer("/params/delta").and_then(Value::as_str) {
                                output.push_str(delta);
                            }
                        }
                        Some("item/completed") => {
                            if let Some(text) = extract_app_server_text(&message) {
                                completed_item_text = text;
                            }
                        }
                        Some("turn/completed") => {
                            let final_text = if output.trim().is_empty() { completed_item_text } else { output };
                            return Ok(compact_text(&final_text, 4000));
                        }
                        _ => {}
                    }
                }
                AppServerMessage::Ping(payload) => writer.send(AppServerMessage::Pong(payload)).await.map_err(|error| error.to_string())?,
                AppServerMessage::Close(_) => return Err("Codex app-server closed the worker connection".into()),
                _ => {}
            }
        }
        Err("Codex app-server stream ended before the turn completed".into())
    })
    .await
    .map_err(|_| "Codex app-server turn timed out after 10 minutes".to_string())?
}

async fn send_app_server_message<S>(writer: &mut S, message: Value) -> Result<(), String>
where
    S: futures_util::Sink<AppServerMessage> + Unpin,
    S::Error: std::fmt::Display,
{
    writer
        .send(AppServerMessage::Text(message.to_string().into()))
        .await
        .map_err(|error| error.to_string())
}

fn extract_app_server_text(message: &Value) -> Option<String> {
    let item = message.pointer("/params/item")?;
    item.get("text")
        .and_then(Value::as_str)
        .map(String::from)
        .or_else(|| {
            item.get("content")
                .and_then(Value::as_str)
                .map(String::from)
        })
}

fn compact_text(text: &str, limit: usize) -> String {
    let normalized = text.split_whitespace().collect::<Vec<_>>().join(" ");
    if normalized.chars().count() <= limit {
        normalized
    } else {
        format!("{}…", normalized.chars().take(limit).collect::<String>())
    }
}

fn agent_role(agent_id: &str) -> &'static str {
    match agent_id {
        "planner" => "the planning and decomposition specialist",
        "researcher" => "the research and requirements specialist",
        "builder" => "the implementation specialist",
        "reviewer" => "the review and quality specialist",
        _ => "a software worker",
    }
}

async fn run_independent_agent(
    state: SharedState,
    project_id: Uuid,
    task_id: Uuid,
    agent_id: String,
    selected_agents: Vec<String>,
) {
    let _ = set_agent_status(&state.database, &agent_id, "working", Some(task_id)).await;
    let _ = publish_event(
        &state,
        event(
            project_id,
            task_id,
            "agent_dispatched",
            "Independent work started.",
            Some("orchestrator"),
            Some(&agent_id),
        ),
    )
    .await;

    let messages: Vec<(u64, &str, &str)> = match agent_id.as_str() {
        "planner" => vec![
            (
                220,
                "researcher",
                "Sharing plan assumptions and open questions.",
            ),
            (
                670,
                "builder",
                "Delivering execution plan and work boundaries.",
            ),
            (
                1120,
                "reviewer",
                "Passing acceptance criteria for parallel review.",
            ),
        ],
        "researcher" => vec![
            (
                330,
                "planner",
                "Reporting constraints discovered during research.",
            ),
            (
                760,
                "builder",
                "Sending research findings and implementation references.",
            ),
            (1180, "reviewer", "Flagging risks for validation."),
        ],
        "builder" => vec![
            (
                420,
                "planner",
                "Requesting clarification on the execution path.",
            ),
            (
                840,
                "researcher",
                "Checking the research assumptions before finalizing.",
            ),
            (
                1240,
                "reviewer",
                "Sending the implementation package for review.",
            ),
        ],
        "reviewer" => vec![
            (
                500,
                "builder",
                "Sending review checkpoints while implementation is active.",
            ),
            (
                940,
                "planner",
                "Returning coverage feedback to improve the plan.",
            ),
            (
                1320,
                "orchestrator",
                "Review signal is ready for final coordination.",
            ),
        ],
        _ => vec![],
    };

    let mut elapsed = 0;
    for (at, recipient, message) in messages {
        tokio::time::sleep(Duration::from_millis(at - elapsed)).await;
        elapsed = at;
        if recipient == "orchestrator" || selected_agents.iter().any(|agent| agent == recipient) {
            let _ = publish_event(
                &state,
                event(
                    project_id,
                    task_id,
                    "agent_message",
                    message,
                    Some(&agent_id),
                    Some(recipient),
                ),
            )
            .await;
        }
    }

    let _ = set_agent_status(&state.database, &agent_id, "complete", Some(task_id)).await;
    let _ = publish_event(
        &state,
        event(
            project_id,
            task_id,
            "agent_result",
            "Independent contribution completed and returned to orchestrator.",
            Some(&agent_id),
            Some("orchestrator"),
        ),
    )
    .await;
}

async fn set_agent_status(
    database: &PgPool,
    agent_id: &str,
    status: &str,
    task_id: Option<Uuid>,
) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE agents SET status = $2, active_task_id = $3 WHERE id = $1")
        .bind(agent_id)
        .bind(status)
        .bind(task_id)
        .execute(database)
        .await?;
    Ok(())
}

fn event(
    project_id: Uuid,
    task_id: Uuid,
    kind: &str,
    message: &str,
    from_agent_id: Option<&str>,
    to_agent_id: Option<&str>,
) -> SwarmEvent {
    SwarmEvent {
        id: Uuid::new_v4(),
        project_id,
        task_id: Some(task_id),
        kind: kind.into(),
        message: message.into(),
        from_agent_id: from_agent_id.map(String::from),
        to_agent_id: to_agent_id.map(String::from),
        created_at: Utc::now(),
    }
}

async fn publish_event(state: &SharedState, event: SwarmEvent) -> ApiResult<()> {
    sqlx::query("INSERT INTO swarm_events (id, project_id, task_id, kind, message, from_agent_id, to_agent_id, created_at) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)")
        .bind(event.id).bind(event.project_id).bind(event.task_id).bind(&event.kind).bind(&event.message).bind(&event.from_agent_id).bind(&event.to_agent_id).bind(event.created_at).execute(&state.database).await.map_err(database_error)?;
    let payload = serde_json::to_string(&event)
        .map_err(|error| (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()))?;
    let mut connection = state
        .redis
        .get_multiplexed_async_connection()
        .await
        .map_err(redis_error)?;
    connection
        .publish::<_, _, i64>("godview.events", payload)
        .await
        .map_err(redis_error)?;
    Ok(())
}

async fn relay_redis_events(redis: redis::Client, event_tx: broadcast::Sender<SwarmEvent>) {
    let Ok(mut subscriber) = redis.get_async_pubsub().await else {
        return;
    };
    if subscriber.subscribe("godview.events").await.is_err() {
        return;
    }
    let mut messages = subscriber.on_message();
    while let Some(message) = messages.next().await {
        if let Ok(payload) = message.get_payload::<String>() {
            if let Ok(event) = serde_json::from_str::<SwarmEvent>(&payload) {
                let _ = event_tx.send(event);
            }
        }
    }
}

async fn websocket_handler(
    ws: WebSocketUpgrade,
    State(state): State<SharedState>,
) -> impl IntoResponse {
    ws.on_upgrade(move |socket| websocket_session(socket, state))
}

async fn websocket_session(socket: WebSocket, state: SharedState) {
    let (mut sender, mut receiver) = socket.split();
    let mut event_rx = state.event_tx.subscribe();
    let send_events = tokio::spawn(async move {
        while let Ok(event) = event_rx.recv().await {
            if sender
                .send(Message::Text(serde_json::to_string(&event).unwrap()))
                .await
                .is_err()
            {
                break;
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

async fn load_projects(database: &PgPool) -> ApiResult<Vec<Project>> {
    let project_rows = sqlx::query_as::<_, ProjectRow>(
        "SELECT id, name, description, created_at FROM projects ORDER BY created_at DESC",
    )
    .fetch_all(database)
    .await
    .map_err(database_error)?;
    let task_rows = sqlx::query_as::<_, TaskRow>("SELECT id, project_id, title, description, assigned_agent_ids, status, result, completed_at, run_started_at, created_at FROM tasks ORDER BY created_at DESC").fetch_all(database).await.map_err(database_error)?;
    let mut tasks_by_project: HashMap<Uuid, Vec<Task>> = HashMap::new();
    for task in task_rows {
        tasks_by_project
            .entry(task.project_id)
            .or_default()
            .push(Task {
                id: task.id,
                title: task.title,
                description: task.description,
                assigned_agent_ids: task.assigned_agent_ids,
                status: task.status,
                result: task.result,
                completed_at: task.completed_at,
                run_started_at: task.run_started_at,
                created_at: task.created_at,
            });
    }
    Ok(project_rows
        .into_iter()
        .map(|project| Project {
            id: project.id,
            name: project.name,
            description: project.description,
            created_at: project.created_at,
            tasks: tasks_by_project.remove(&project.id).unwrap_or_default(),
        })
        .collect())
}

async fn load_agents(database: &PgPool) -> ApiResult<Vec<Agent>> {
    sqlx::query_as::<_, Agent>("SELECT id, name, role, description, status, active_task_id, x, y FROM agents ORDER BY name").fetch_all(database).await.map_err(database_error)
}

async fn load_events(database: &PgPool, limit: i64) -> ApiResult<Vec<SwarmEvent>> {
    sqlx::query_as::<_, SwarmEvent>("SELECT id, project_id, task_id, kind, message, from_agent_id, to_agent_id, created_at FROM swarm_events ORDER BY created_at DESC LIMIT $1").bind(limit).fetch_all(database).await.map_err(database_error)
}

async fn seed_agents(database: &PgPool) -> Result<(), sqlx::Error> {
    for agent in [
        (
            "orchestrator",
            "ORCHESTRATOR",
            "Master control",
            "Routes work and coordinates the swarm.",
            45.0_f32,
            46.0_f32,
        ),
        (
            "planner",
            "ATLAS",
            "Strategist",
            "Decomposes work into an executable plan.",
            28.0,
            28.0,
        ),
        (
            "researcher",
            "RAVEN",
            "Research",
            "Finds requirements and useful context.",
            67.0,
            25.0,
        ),
        (
            "builder",
            "FORGE",
            "Builder",
            "Implements the proposed solution.",
            74.0,
            66.0,
        ),
        (
            "reviewer",
            "SENTINEL",
            "Verifier",
            "Reviews output and tests edge cases.",
            24.0,
            68.0,
        ),
    ] {
        sqlx::query("INSERT INTO agents (id, name, role, description, x, y) VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (id) DO NOTHING").bind(agent.0).bind(agent.1).bind(agent.2).bind(agent.3).bind(agent.4).bind(agent.5).execute(database).await?;
    }
    Ok(())
}

async fn recover_interrupted_runs(database: &PgPool) -> Result<(), sqlx::Error> {
    sqlx::query("UPDATE tasks SET status = 'ready' WHERE status = 'running'")
        .execute(database)
        .await?;
    sqlx::query("UPDATE agents SET status = 'idle', active_task_id = NULL WHERE status != 'idle'")
        .execute(database)
        .await?;
    Ok(())
}

fn database_error(error: sqlx::Error) -> (StatusCode, String) {
    eprintln!("database error: {error}");
    (
        StatusCode::INTERNAL_SERVER_ERROR,
        "Database operation failed".into(),
    )
}
fn redis_error(error: redis::RedisError) -> (StatusCode, String) {
    eprintln!("redis error: {error}");
    (
        StatusCode::SERVICE_UNAVAILABLE,
        "Redis event relay is unavailable".into(),
    )
}
