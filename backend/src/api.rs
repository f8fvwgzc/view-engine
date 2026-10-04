//! HTTP API: projects, tasks, runs (with their born agents), memory, skills, settings and providers.

use std::{
    collections::HashMap,
    sync::{atomic::{AtomicBool, Ordering}, Arc},
};

use axum::{
    extract::{Path, Query, State},
    http::StatusCode,
    Json,
};
use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use uuid::Uuid;

use crate::{
    events::{publish, SwarmEvent},
    harness::{memory, orchestrator, providers, settings},
    SharedState,
};

pub type ApiResult<T> = Result<T, (StatusCode, String)>;

fn db_error(error: sqlx::Error) -> (StatusCode, String) {
    tracing::error!("database error: {error}");
    (StatusCode::INTERNAL_SERVER_ERROR, "Database operation failed".into())
}

fn not_found(what: &str) -> (StatusCode, String) {
    (StatusCode::NOT_FOUND, format!("{what} not found"))
}

#[derive(Clone, Serialize, sqlx::FromRow)]
pub struct Task {
    pub id: Uuid,
    pub project_id: Uuid,
    pub title: String,
    pub description: String,
    pub status: String,
    pub config: Value,
    pub result: Option<String>,
    pub latest_run_id: Option<Uuid>,
    pub run_started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
}

const TASK_COLUMNS: &str = "id, project_id, title, description, status, config, result, latest_run_id, run_started_at, completed_at, created_at";

#[derive(Clone, Serialize, sqlx::FromRow)]
pub struct ProjectRow {
    pub id: Uuid,
    pub name: String,
    pub description: String,
    pub created_at: DateTime<Utc>,
}

#[derive(Serialize)]
pub struct Project {
    #[serde(flatten)]
    pub row: ProjectRow,
    pub tasks: Vec<Task>,
    pub memory_count: i64,
}

#[derive(Clone, Serialize, sqlx::FromRow)]
pub struct Run {
    pub id: Uuid,
    pub project_id: Uuid,
    pub task_id: Uuid,
    pub status: String,
    pub round: i32,
    pub provider: String,
    pub model: Option<String>,
    pub config: Value,
    pub intent: Option<Value>,
    pub report: Option<String>,
    pub decision: Option<Value>,
    pub market: Option<Value>,
    pub error: Option<String>,
    pub tokens_in: i64,
    pub tokens_out: i64,
    pub tokens_cache_read: i64,
    pub tokens_cache_write: i64,
    pub tokens_saved: i64,
    pub llm_calls: i32,
    pub cache_hits: i32,
    pub started_at: DateTime<Utc>,
    pub completed_at: Option<DateTime<Utc>>,
}

const RUN_COLUMNS: &str = "id, project_id, task_id, status, round, provider, model, config, intent, report, decision, market, error, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write, tokens_saved, llm_calls, cache_hits, started_at, completed_at";

async fn load_projects(state: &SharedState) -> ApiResult<Vec<Project>> {
    let rows = sqlx::query_as::<_, ProjectRow>("SELECT id, name, description, created_at FROM projects ORDER BY created_at DESC").fetch_all(&state.database).await.map_err(db_error)?;
    let tasks = sqlx::query_as::<_, Task>(&format!("SELECT {TASK_COLUMNS} FROM tasks ORDER BY created_at DESC")).fetch_all(&state.database).await.map_err(db_error)?;
    let counts: Vec<(Uuid, i64)> = sqlx::query_as("SELECT project_id, count(*) FROM memories WHERE superseded_by IS NULL GROUP BY project_id").fetch_all(&state.database).await.map_err(db_error)?;
    let counts: HashMap<Uuid, i64> = counts.into_iter().collect();
    let mut by_project: HashMap<Uuid, Vec<Task>> = HashMap::new();
    for task in tasks {
        by_project.entry(task.project_id).or_default().push(task);
    }
    Ok(rows.into_iter().map(|row| Project { tasks: by_project.remove(&row.id).unwrap_or_default(), memory_count: counts.get(&row.id).copied().unwrap_or(0), row }).collect())
}

pub async fn dashboard(State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    let projects = load_projects(&state).await?;
    let events = sqlx::query_as::<_, SwarmEvent>("SELECT id, project_id, task_id, run_id, kind, message, from_agent_id, to_agent_id, data, created_at FROM swarm_events ORDER BY created_at DESC LIMIT 200")
        .fetch_all(&state.database)
        .await
        .map_err(db_error)?;
    Ok(Json(json!({"projects": projects, "events": events, "skills": state.harness.skills.list().await.len()})))
}

#[derive(Deserialize)]
pub struct CreateProject {
    name: String,
    #[serde(default)]
    description: String,
}

pub async fn create_project(State(state): State<SharedState>, Json(payload): Json<CreateProject>) -> ApiResult<Json<Project>> {
    let name = payload.name.trim();
    if name.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "Project name is required".into()));
    }
    let row = sqlx::query_as::<_, ProjectRow>("INSERT INTO projects (id, name, description) VALUES ($1, $2, $3) RETURNING id, name, description, created_at")
        .bind(Uuid::new_v4())
        .bind(name)
        .bind(payload.description.trim())
        .fetch_one(&state.database)
        .await
        .map_err(db_error)?;
    Ok(Json(Project { row, tasks: vec![], memory_count: 0 }))
}

pub async fn delete_project(Path(project_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<StatusCode> {
    let result = sqlx::query("DELETE FROM projects WHERE id = $1").bind(project_id).execute(&state.database).await.map_err(db_error)?;
    if result.rows_affected() == 0 { Err(not_found("Project")) } else { Ok(StatusCode::NO_CONTENT) }
}

#[derive(Deserialize)]
pub struct CreateTask {
    title: String,
    #[serde(default)]
    description: String,
    #[serde(default)]
    config: Value,
}

pub async fn create_task(Path(project_id): Path<Uuid>, State(state): State<SharedState>, Json(payload): Json<CreateTask>) -> ApiResult<Json<Task>> {
    let title = payload.title.trim();
    if title.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "Task title is required".into()));
    }
    let config = if payload.config.is_object() { payload.config } else { json!({}) };
    sqlx::query_as::<_, Task>(&format!("INSERT INTO tasks (id, project_id, title, description, status, config) VALUES ($1, $2, $3, $4, 'ready', $5) RETURNING {TASK_COLUMNS}"))
        .bind(Uuid::new_v4())
        .bind(project_id)
        .bind(title)
        .bind(payload.description.trim())
        .bind(config)
        .fetch_one(&state.database)
        .await
        .map_err(|error| match &error {
            sqlx::Error::Database(database) if database.is_foreign_key_violation() => not_found("Project"),
            _ => db_error(error),
        })
        .map(Json)
}

#[derive(Deserialize)]
pub struct UpdateTask {
    title: Option<String>,
    description: Option<String>,
    config: Option<Value>,
}

pub async fn update_task(Path(task_id): Path<Uuid>, State(state): State<SharedState>, Json(payload): Json<UpdateTask>) -> ApiResult<Json<Task>> {
    sqlx::query_as::<_, Task>(&format!("UPDATE tasks SET title = COALESCE($2, title), description = COALESCE($3, description), config = COALESCE($4, config) WHERE id = $1 RETURNING {TASK_COLUMNS}"))
        .bind(task_id)
        .bind(payload.title)
        .bind(payload.description)
        .bind(payload.config)
        .fetch_optional(&state.database)
        .await
        .map_err(db_error)?
        .map(Json)
        .ok_or_else(|| not_found("Task"))
}

pub async fn delete_task(Path(task_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<StatusCode> {
    let result = sqlx::query("DELETE FROM tasks WHERE id = $1").bind(task_id).execute(&state.database).await.map_err(db_error)?;
    if result.rows_affected() == 0 { Err(not_found("Task")) } else { Ok(StatusCode::NO_CONTENT) }
}

pub async fn run_task(Path(task_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<Json<Run>> {
    let task = sqlx::query_as::<_, Task>(&format!("SELECT {TASK_COLUMNS} FROM tasks WHERE id = $1")).bind(task_id).fetch_optional(&state.database).await.map_err(db_error)?.ok_or_else(|| not_found("Task"))?;
    if task.status == "running" {
        if let Some(run_id) = task.latest_run_id {
            if state.active_runs.lock().await.contains_key(&run_id) {
                return Err((StatusCode::CONFLICT, "This task is already running".into()));
            }
        }
    }
    let run_settings = settings::RunSettings::load(&state.database, &task.config).await;
    let run = sqlx::query_as::<_, Run>(&format!("INSERT INTO runs (id, project_id, task_id, provider, model, status) VALUES ($1, $2, $3, $4, $5, 'planning') RETURNING {RUN_COLUMNS}"))
        .bind(Uuid::new_v4())
        .bind(task.project_id)
        .bind(task.id)
        .bind(run_settings.provider.as_str())
        .bind(&run_settings.model)
        .fetch_one(&state.database)
        .await
        .map_err(db_error)?;
    sqlx::query("UPDATE tasks SET status = 'running', result = NULL, completed_at = NULL, run_started_at = NOW(), latest_run_id = $2 WHERE id = $1").bind(task.id).bind(run.id).execute(&state.database).await.map_err(db_error)?;
    let cancelled = Arc::new(AtomicBool::new(false));
    state.active_runs.lock().await.insert(run.id, cancelled.clone());
    tokio::spawn(orchestrator::execute_run(state.clone(), run.id, cancelled));
    Ok(Json(run))
}

pub async fn cancel_run(Path(run_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    match state.active_runs.lock().await.get(&run_id) {
        Some(flag) => {
            flag.store(true, Ordering::SeqCst);
            Ok(Json(json!({"cancelling": true})))
        }
        None => Err((StatusCode::CONFLICT, "Run is not active".into())),
    }
}

pub async fn task_runs(Path(task_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<Json<Vec<Run>>> {
    sqlx::query_as::<_, Run>(&format!("SELECT {RUN_COLUMNS} FROM runs WHERE task_id = $1 ORDER BY started_at DESC LIMIT 20")).bind(task_id).fetch_all(&state.database).await.map_err(db_error).map(Json)
}

pub async fn get_run(Path(run_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    let run = sqlx::query_as::<_, Run>(&format!("SELECT {RUN_COLUMNS} FROM runs WHERE id = $1")).bind(run_id).fetch_optional(&state.database).await.map_err(db_error)?.ok_or_else(|| not_found("Run"))?;
    let agents = sqlx::query_as::<_, orchestrator::RunAgent>(&format!("SELECT {} FROM run_agents WHERE run_id = $1 ORDER BY created_at", orchestrator::AGENT_COLUMNS))
        .bind(run_id)
        .fetch_all(&state.database)
        .await
        .map_err(db_error)?;
    let events = sqlx::query_as::<_, SwarmEvent>("SELECT id, project_id, task_id, run_id, kind, message, from_agent_id, to_agent_id, data, created_at FROM swarm_events WHERE run_id = $1 ORDER BY created_at DESC LIMIT 400")
        .bind(run_id)
        .fetch_all(&state.database)
        .await
        .map_err(db_error)?;
    let active = state.active_runs.lock().await.contains_key(&run_id);
    Ok(Json(json!({"run": run, "agents": agents, "events": events, "active": active})))
}

pub async fn list_events(State(state): State<SharedState>) -> ApiResult<Json<Vec<SwarmEvent>>> {
    sqlx::query_as::<_, SwarmEvent>("SELECT id, project_id, task_id, run_id, kind, message, from_agent_id, to_agent_id, data, created_at FROM swarm_events ORDER BY created_at DESC LIMIT 200").fetch_all(&state.database).await.map_err(db_error).map(Json)
}

/// External producers (LangGraph workers, Claude Code hooks, scripts) can push events into the live stream.
pub async fn ingest_event(State(state): State<SharedState>, Json(mut event): Json<SwarmEvent>) -> ApiResult<Json<SwarmEvent>> {
    if event.id.is_nil() {
        event.id = Uuid::new_v4();
    }
    publish(&state, event.clone()).await.map_err(|error| (StatusCode::BAD_REQUEST, error))?;
    Ok(Json(event))
}

#[derive(Deserialize)]
pub struct MemoryQuery {
    q: Option<String>,
    limit: Option<i64>,
}

pub async fn project_memory(Path(project_id): Path<Uuid>, Query(query): Query<MemoryQuery>, State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    let limit = query.limit.unwrap_or(50).clamp(1, 200);
    let items = match query.q.as_deref().filter(|text| !text.trim().is_empty()) {
        Some(text) => serde_json::to_value(memory::recall(&state.harness, project_id, text, limit).await).unwrap_or_default(),
        None => json!(memory::list(&state.database, project_id, limit).await),
    };
    Ok(Json(json!({"embed_model": state.harness.embedder.model(), "items": items})))
}

pub async fn list_skills(State(state): State<SharedState>) -> Json<Value> {
    Json(json!(state.harness.skills.list().await))
}

pub async fn get_skill(Path(name): Path<String>, State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    state.harness.skills.get(&name).await.map(|skill| Json(json!(skill))).ok_or_else(|| not_found("Skill"))
}

#[derive(Deserialize)]
pub struct SaveSkill {
    markdown: String,
}

pub async fn save_skill(Path(name): Path<String>, State(state): State<SharedState>, Json(payload): Json<SaveSkill>) -> ApiResult<Json<Value>> {
    state.harness.skills.save(&name, &payload.markdown).await.map(|skill| Json(json!(skill))).map_err(|error| (StatusCode::BAD_REQUEST, error))
}

pub async fn reload_skills(State(state): State<SharedState>) -> Json<Value> {
    Json(json!({"loaded": state.harness.skills.reload().await}))
}

pub async fn get_settings(State(state): State<SharedState>) -> Json<Value> {
    Json(settings::get_global(&state.database).await)
}

pub async fn put_settings(State(state): State<SharedState>, Json(value): Json<Value>) -> ApiResult<Json<Value>> {
    settings::put_global(&state.database, &value).await.map(Json).map_err(|error| (StatusCode::BAD_REQUEST, error))
}

pub async fn list_providers(State(state): State<SharedState>) -> Json<Value> {
    let harness = &state.harness;
    let mut items = Vec::new();
    for kind in providers::ProviderKind::all() {
        items.push(providers::probe(&harness.providers, &harness.http, kind).await);
    }
    Json(json!({"providers": items, "embed_model": harness.embedder.model(), "limits": *harness.limits.read().await}))
}

// ── Trading desk ─────────────────────────────────────────────────────────────

const MAX_UPLOAD_BYTES: usize = 12 * 1024 * 1024;

/// Chart screenshots for a task (PNG/JPEG/WebP/GIF), stored under the agent workdir so the chart reader can open them.
pub async fn upload_attachment(Path(task_id): Path<Uuid>, State(state): State<SharedState>, mut multipart: axum::extract::Multipart) -> ApiResult<Json<Vec<Value>>> {
    let exists: Option<Uuid> = sqlx::query_scalar("SELECT id FROM tasks WHERE id = $1").bind(task_id).fetch_optional(&state.database).await.map_err(db_error)?;
    exists.ok_or_else(|| not_found("Task"))?;
    let dir = state.harness.providers.workdir.join("uploads").join(task_id.to_string());
    tokio::fs::create_dir_all(&dir).await.map_err(|error| (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()))?;
    let mut saved = Vec::new();
    while let Some(field) = multipart.next_field().await.map_err(|error| (StatusCode::BAD_REQUEST, error.to_string()))? {
        let filename = field.file_name().unwrap_or("chart.png").to_string();
        let mime = field.content_type().unwrap_or("application/octet-stream").to_string();
        let extension = match mime.as_str() {
            "image/png" => "png",
            "image/jpeg" => "jpg",
            "image/webp" => "webp",
            "image/gif" => "gif",
            _ => return Err((StatusCode::UNSUPPORTED_MEDIA_TYPE, format!("{filename}: only PNG, JPEG, WebP or GIF chart images are accepted"))),
        };
        let bytes = field.bytes().await.map_err(|error| (StatusCode::BAD_REQUEST, error.to_string()))?;
        if bytes.len() > MAX_UPLOAD_BYTES {
            return Err((StatusCode::PAYLOAD_TOO_LARGE, format!("{filename} is larger than 12 MB")));
        }
        let id = Uuid::new_v4();
        let path = dir.join(format!("{id}.{extension}"));
        tokio::fs::write(&path, &bytes).await.map_err(|error| (StatusCode::INTERNAL_SERVER_ERROR, error.to_string()))?;
        sqlx::query("INSERT INTO attachments (id, task_id, filename, path, mime, bytes) VALUES ($1, $2, $3, $4, $5, $6)")
            .bind(id)
            .bind(task_id)
            .bind(&filename)
            .bind(path.display().to_string())
            .bind(&mime)
            .bind(bytes.len() as i64)
            .execute(&state.database)
            .await
            .map_err(db_error)?;
        saved.push(json!({"id": id, "filename": filename, "mime": mime, "bytes": bytes.len()}));
    }
    Ok(Json(saved))
}

pub async fn list_attachments(Path(task_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    let rows: Vec<(Uuid, String, String, i64, DateTime<Utc>)> = sqlx::query_as("SELECT id, filename, mime, bytes, created_at FROM attachments WHERE task_id = $1 ORDER BY created_at")
        .bind(task_id)
        .fetch_all(&state.database)
        .await
        .map_err(db_error)?;
    Ok(Json(json!(rows.into_iter().map(|(id, filename, mime, bytes, created_at)| json!({"id": id, "filename": filename, "mime": mime, "bytes": bytes, "created_at": created_at})).collect::<Vec<_>>())))
}

pub async fn get_attachment(Path(id): Path<Uuid>, State(state): State<SharedState>) -> Result<axum::response::Response, (StatusCode, String)> {
    let row: Option<(String, String)> = sqlx::query_as("SELECT path, mime FROM attachments WHERE id = $1").bind(id).fetch_optional(&state.database).await.map_err(db_error)?;
    let (path, mime) = row.ok_or_else(|| not_found("Attachment"))?;
    let bytes = tokio::fs::read(&path).await.map_err(|_| not_found("Attachment file"))?;
    Ok(axum::response::Response::builder()
        .header(axum::http::header::CONTENT_TYPE, mime)
        .header(axum::http::header::CACHE_CONTROL, "private, max-age=3600")
        .body(axum::body::Body::from(bytes))
        .expect("valid response"))
}

pub async fn delete_attachment(Path(id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<StatusCode> {
    let path: Option<String> = sqlx::query_scalar("DELETE FROM attachments WHERE id = $1 RETURNING path").bind(id).fetch_optional(&state.database).await.map_err(db_error)?;
    let path = path.ok_or_else(|| not_found("Attachment"))?;
    let _ = tokio::fs::remove_file(path).await;
    Ok(StatusCode::NO_CONTENT)
}

#[derive(Deserialize)]
pub struct PredictionQuery {
    project_id: Option<Uuid>,
}

pub async fn list_predictions(Query(query): Query<PredictionQuery>, State(state): State<SharedState>) -> Json<Value> {
    Json(crate::harness::market::list_predictions(&state.database, query.project_id).await)
}

pub async fn market_health(State(state): State<SharedState>) -> Json<Value> {
    Json(state.harness.quant.health().await)
}

pub async fn market_symbols(State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    state.harness.quant.symbols().await.map(Json).map_err(|error| (StatusCode::SERVICE_UNAVAILABLE, error))
}

#[derive(Deserialize)]
pub struct WatchRequest {
    symbol: String,
    #[serde(default)]
    intervals: Vec<String>,
}

pub async fn list_watchlist(Path(project_id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<Json<Value>> {
    let rows: Vec<(Uuid, String, Vec<String>, bool, Option<DateTime<Utc>>, Option<String>)> = sqlx::query_as("SELECT id, symbol, intervals, active, last_checked_at, last_error FROM watchlist WHERE project_id = $1 ORDER BY created_at")
        .bind(project_id)
        .fetch_all(&state.database)
        .await
        .map_err(db_error)?;
    Ok(Json(json!(rows.into_iter().map(|(id, symbol, intervals, active, checked, error)| json!({"id": id, "symbol": symbol, "intervals": intervals, "active": active, "last_checked_at": checked, "last_error": error})).collect::<Vec<_>>())))
}

pub async fn add_watch(Path(project_id): Path<Uuid>, State(state): State<SharedState>, Json(payload): Json<WatchRequest>) -> ApiResult<Json<Value>> {
    let symbol = payload.symbol.trim().to_uppercase().replace(['/', ' '], "");
    if symbol.is_empty() {
        return Err((StatusCode::BAD_REQUEST, "symbol is required".into()));
    }
    let intervals = if payload.intervals.is_empty() { vec!["4h".to_string(), "1h".to_string()] } else { payload.intervals };
    let id: Uuid = sqlx::query_scalar("INSERT INTO watchlist (id, project_id, symbol, intervals) VALUES ($1, $2, $3, $4) ON CONFLICT (project_id, symbol) DO UPDATE SET intervals = EXCLUDED.intervals, active = TRUE RETURNING id")
        .bind(Uuid::new_v4())
        .bind(project_id)
        .bind(&symbol)
        .bind(&intervals)
        .fetch_one(&state.database)
        .await
        .map_err(db_error)?;
    Ok(Json(json!({"id": id, "symbol": symbol, "intervals": intervals, "active": true})))
}

pub async fn delete_watch(Path(id): Path<Uuid>, State(state): State<SharedState>) -> ApiResult<StatusCode> {
    sqlx::query("DELETE FROM watchlist WHERE id = $1").bind(id).execute(&state.database).await.map_err(db_error)?;
    Ok(StatusCode::NO_CONTENT)
}
