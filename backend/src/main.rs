//! View Engine API: a research harness where one orchestrator hires agents, runs them as a dependency graph,
//! and turns their web research into a decision-ready strategy. See `harness/` for the engine.

mod api;
mod events;
mod harness;

use std::{collections::HashMap, env, path::PathBuf, sync::{atomic::AtomicBool, Arc}};

use axum::{
    http::Method,
    routing::{get, post},
    Json, Router,
};
use sqlx::postgres::PgPoolOptions;
use tokio::sync::{broadcast, Mutex};
use tower_http::{cors::{Any, CorsLayer}, trace::TraceLayer};
use uuid::Uuid;

use crate::{
    events::SwarmEvent,
    harness::{memory::Embedder, providers::ProviderConfig, skills::SkillLibrary, web, Harness},
};

pub struct AppState {
    pub database: sqlx::PgPool,
    pub redis: redis::Client,
    pub event_tx: broadcast::Sender<SwarmEvent>,
    pub harness: Arc<Harness>,
    /// Cancellation flags of runs executing in this process.
    pub active_runs: Mutex<HashMap<Uuid, Arc<AtomicBool>>>,
}

pub type SharedState = Arc<AppState>;

#[tokio::main]
async fn main() {
    tracing_subscriber::fmt().with_env_filter(tracing_subscriber::EnvFilter::try_from_default_env().unwrap_or_else(|_| "view_engine_api=info,tower_http=warn".into())).init();

    let database_url = env::var("DATABASE_URL").unwrap_or_else(|_| "postgres://viewengine:viewengine@localhost:5433/viewengine".into());
    let redis_url = env::var("REDIS_URL").unwrap_or_else(|_| "redis://127.0.0.1:6379".into());
    let port: u16 = env::var("PORT").ok().and_then(|value| value.parse().ok()).unwrap_or(3001);
    // The skill library is user data next to the repo (git-ignored): the engine ships without skills.
    let skills_dir = env::var("SKILLS_DIR").map(PathBuf::from).unwrap_or_else(|_| PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("skills"));
    // Local tool with no authentication: listen on localhost unless BIND_ADDR says otherwise (containers use 0.0.0.0).
    let bind_addr = env::var("BIND_ADDR").unwrap_or_else(|_| "127.0.0.1".into());

    let database = PgPoolOptions::new()
        .max_connections(20)
        .connect(&database_url)
        .await
        .expect("Postgres is required. Start it with `docker compose up -d postgres redis`.");
    sqlx::migrate!("./migrations").run(&database).await.expect("Postgres migration failed");
    // Runs cannot survive a restart of this process (agents are child processes); mark them interrupted.
    let _ = sqlx::query("UPDATE runs SET status = 'interrupted', completed_at = NOW() WHERE status IN ('planning', 'running')").execute(&database).await;
    let _ = sqlx::query("UPDATE tasks SET status = 'ready' WHERE status = 'running'").execute(&database).await;
    let _ = sqlx::query("UPDATE run_agents SET status = 'skipped', error = 'interrupted by restart' WHERE status IN ('pending', 'running', 'retrying')").execute(&database).await;

    let redis = redis::Client::open(redis_url).expect("Invalid REDIS_URL");
    let (event_tx, _) = broadcast::channel(1024);
    tokio::spawn(events::relay_redis_events(redis.clone(), event_tx.clone()));

    let http = web::client();
    let providers = ProviderConfig::from_env();
    let embedder = Embedder::detect(&http, &providers.ollama_url).await;
    let skills = SkillLibrary::load(skills_dir.clone()).await;
    tracing::info!("{} skills in {} (add your own or import from GitHub in Settings → Skills)", skills.len().await, skills_dir.display());
    let harness = Arc::new(Harness { database: database.clone(), http, providers, skills, embedder, limits: Default::default(), quant: harness::market::Quant::from_env(web::client()), rlcd: harness::rlcd::Rlcd::from_env(web::client()) });

    harness.refresh_skill_priors().await;
    let state = Arc::new(AppState { database, redis, event_tx, harness, active_runs: Mutex::new(HashMap::new()) });
    tokio::spawn(harness::market::evaluator(state.clone()));
    tokio::spawn(harness::market::watcher(state.clone()));
    tokio::spawn(harness::rlcd::session_scanner(state.clone()));
    let app = Router::new()
        .route("/api/health", get(|| async { Json(serde_json::json!({"status": "ok", "engine": "view-engine"})) }))
        .route("/api/dashboard", get(api::dashboard))
        .route("/api/projects", post(api::create_project))
        .route("/api/projects/:project_id", axum::routing::delete(api::delete_project))
        .route("/api/projects/:project_id/tasks", post(api::create_task))
        .route("/api/projects/:project_id/memory", get(api::project_memory))
        .route("/api/tasks/:task_id", axum::routing::patch(api::update_task).delete(api::delete_task))
        .route("/api/tasks/:task_id/run", post(api::run_task))
        .route("/api/tasks/:task_id/runs", get(api::task_runs))
        .route("/api/tasks/:task_id/attachments", get(api::list_attachments).post(api::upload_attachment))
        .route("/api/attachments/:id", get(api::get_attachment).delete(api::delete_attachment))
        .route("/api/predictions", get(api::list_predictions))
        .route("/api/projects/:project_id/watchlist", get(api::list_watchlist).post(api::add_watch))
        .route("/api/watchlist/:id", axum::routing::delete(api::delete_watch))
        .route("/api/market/health", get(api::market_health))
        .route("/api/market/symbols", get(api::market_symbols))
        .route("/api/market/backtest", get(api::market_backtest))
        .route("/api/market/retest", get(api::market_retest))
        .route("/api/market/levels", get(api::market_levels))
        .route("/api/market/mtf", get(api::market_mtf))
        .route("/api/market/chart", get(api::market_chart))
        .route("/api/rlcd/health", get(api::rlcd_health))
        .route("/api/rlcd/heads", get(api::rlcd_heads))
        .route("/api/rlcd/calibration", get(api::rlcd_calibration))
        .route("/api/rlcd/decide", post(api::rlcd_decide))
        .route("/api/rlcd/scan", post(api::rlcd_scan))
        .route("/api/rlcd/bernoulli", get(api::rlcd_bernoulli))
        .route("/api/rlcd/train", post(api::rlcd_train))
        .route("/api/rlcd/train/:job", get(api::rlcd_train_status))
        .route("/api/runs/:run_id", get(api::get_run))
        .route("/api/runs/:run_id/cancel", post(api::cancel_run))
        .route("/api/events", get(api::list_events).post(api::ingest_event))
        .route("/api/skills", get(api::list_skills))
        .route("/api/skills/reload", post(api::reload_skills))
        .route("/api/skills/import", post(api::import_skills))
        .route("/api/skills/:name", get(api::get_skill).put(api::save_skill).delete(api::delete_skill))
        .route("/api/settings", get(api::get_settings).put(api::put_settings))
        .route("/api/providers", get(api::list_providers))
        .route("/ws", get(events::websocket_handler))
        .layer(CorsLayer::new().allow_origin(Any).allow_methods([Method::GET, Method::POST, Method::PATCH, Method::PUT, Method::DELETE]).allow_headers(Any))
        .layer(axum::extract::DefaultBodyLimit::max(40 * 1024 * 1024))
        .layer(TraceLayer::new_for_http())
        .with_state(state);

    let listener = tokio::net::TcpListener::bind((bind_addr.as_str(), port)).await.unwrap_or_else(|error| panic!("Could not bind port {port}: {error}"));
    tracing::info!("View Engine API listening on http://localhost:{port}");
    axum::serve(listener, app).await.expect("View Engine API stopped unexpectedly");
}
