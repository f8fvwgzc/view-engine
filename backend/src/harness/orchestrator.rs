//! The orchestrator: the only agent that exists when a run starts. It hires every other agent
//! (Paperclip-style hiring with org chart + skills), wires them into a dependency graph, and runs it.
//!
//! - Graph engineering: agents form a DAG through `depends_on`; validated with Kahn's algorithm; results flow along
//!   edges as compact reports (Paperclip `childIssueSummaries`: capped summaries, full output kept in the DB).
//! - Loop engineering: each agent iterates (act → observe → decide `continue`/`done`) within its iteration
//!   limits; continuation prompts carry a compacted summary instead of the full transcript (Codex compaction idea).
//!   The critic closes an outer loop: `revise` hires follow-up specialists and re-runs the strategist (bounded rounds).
//! - Harness engineering: concurrency-limited scheduler with per-node retry + backoff (Symphony/texc-symphony
//!   `retry_delay`), per-agent timeouts, cancellation, and the model router's fallback when a subscription hits its usage limit.

use std::{
    collections::{BTreeMap, HashMap, HashSet, VecDeque},
    sync::{atomic::{AtomicBool, Ordering}, Arc},
    time::Duration,
};

use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tokio::{sync::RwLock, task::JoinSet};
use uuid::Uuid;

use super::{
    compress::{compress_text, estimate_tokens, truncate_chars},
    extract_json, ledger,
    memory::{self, NewMemory, Scope},
    prompts::{self, AgentBrief},
    providers::{LlmRequest, Progress, ProgressSink},
    router,
    retry::{retry_delay, DelayType},
    settings::RunSettings,
    web, CallSite,
};
use crate::{events::EventDraft, SharedState};

const ORCHESTRATOR: &str = "orchestrator";
const DEPENDENCY_CHARS: usize = 3_000;
const MAX_RETRY_BACKOFF_MS: u64 = 60_000;
const MAX_HIRE_DEPTH: usize = 2;
/// Long-term memory is always on; these fixed token budgets bound how much of it enters a prompt.
const MEMORY_PLAN_TOKENS: usize = 700;
const MEMORY_AGENT_TOKENS: usize = 350;

#[derive(Debug, Clone, Serialize, Deserialize, sqlx::FromRow)]
pub struct RunAgent {
    pub id: Uuid,
    pub run_id: Uuid,
    pub task_id: Uuid,
    pub key: String,
    pub name: String,
    pub role: String,
    pub kind: String,
    pub objective: String,
    pub reports_to: Option<String>,
    pub hired_by: Option<String>,
    pub skills: Vec<String>,
    pub depends_on: Vec<String>,
    pub round: i32,
    pub status: String,
    pub attempt: i32,
    pub provider: String,
    pub model: Option<String>,
    pub max_iterations: i32,
    pub iterations: i32,
    pub output: Option<String>,
    pub summary: Option<String>,
    pub sources: Value,
    pub tokens_in: i64,
    pub tokens_out: i64,
    pub tokens_cache_read: i64,
    pub tokens_cache_write: i64,
    pub error: Option<String>,
    pub next_retry_at: Option<DateTime<Utc>>,
    pub started_at: Option<DateTime<Utc>>,
    pub completed_at: Option<DateTime<Utc>>,
    pub created_at: DateTime<Utc>,
}

pub const AGENT_COLUMNS: &str = "id, run_id, task_id, key, name, role, kind, objective, reports_to, hired_by, skills, depends_on, round, status, attempt, provider, model, max_iterations, iterations, output, summary, sources, tokens_in, tokens_out, tokens_cache_read, tokens_cache_write, error, next_retry_at, started_at, completed_at, created_at";

/// A hire request, from the orchestrator's plan or from an agent's `hire` field.
#[derive(Debug, Clone, Deserialize)]
pub struct AgentSpec {
    pub key: String,
    pub name: String,
    #[serde(default)]
    pub role: String,
    #[serde(default = "default_kind")]
    pub kind: String,
    pub objective: String,
    #[serde(default)]
    pub skills: Vec<String>,
    #[serde(default)]
    pub depends_on: Vec<String>,
    #[serde(default)]
    pub reports_to: Option<String>,
    #[serde(default)]
    pub max_iterations: Option<i32>,
}

fn default_kind() -> String {
    "specialist".into()
}

struct RunContext {
    state: SharedState,
    run_id: Uuid,
    project_id: Uuid,
    task_id: Uuid,
    project_name: String,
    project_context: String,
    task_title: String,
    task_brief: String,
    settings: RunSettings,
    decision: RwLock<String>,
    /// Orchestrator intent, enriched with the intent agent's summary once it finishes.
    intent: RwLock<String>,
    cancelled: Arc<AtomicBool>,
    /// Set when the task is a trading desk task (instrument, market data pack, chart screenshots).
    trading: RwLock<Option<Trading>>,
}

#[derive(Clone)]
struct Trading {
    symbol: Option<String>,
    horizon_hours: i32,
    market: String,
    model_prob_up: Option<f64>,
    attachments: Vec<std::path::PathBuf>,
    /// Replay mode: the desk analyses this past moment with no knowledge (and no web access) after it.
    as_of: Option<DateTime<Utc>>,
}

impl RunContext {
    fn event(&self, kind: &str, message: impl Into<String>) -> EventDraft {
        EventDraft::new(self.project_id, self.task_id, self.run_id, kind, message)
    }

    fn site(&self, agent_id: Option<Uuid>, cacheable: bool) -> CallSite {
        CallSite { run_id: self.run_id, agent_id, cacheable }
    }
}

/// Entry point, spawned by the API after the run row exists.
pub async fn execute_run(state: SharedState, run_id: Uuid, cancelled: Arc<AtomicBool>) {
    let outcome = run_inner(state.clone(), run_id, cancelled).await;
    if let Err(error) = outcome {
        tracing::error!("run {run_id} failed: {error}");
        let row: Option<(Uuid, Uuid)> = sqlx::query_as("UPDATE runs SET status = 'failed', error = $2, completed_at = NOW() WHERE id = $1 RETURNING project_id, task_id")
            .bind(run_id)
            .bind(&error)
            .fetch_optional(&state.database)
            .await
            .ok()
            .flatten();
        if let Some((project_id, task_id)) = row {
            let _ = sqlx::query("UPDATE tasks SET status = 'failed', result = $2, completed_at = NOW() WHERE id = $1").bind(task_id).bind(format!("Run failed: {error}")).execute(&state.database).await;
            let _ = sqlx::query("UPDATE run_agents SET status = CASE WHEN status IN ('complete','failed','skipped') THEN status ELSE 'skipped' END WHERE run_id = $1").bind(run_id).execute(&state.database).await;
            EventDraft::new(project_id, task_id, run_id, "run_failed", format!("Run failed: {error}")).from(ORCHESTRATOR).publish(&state).await;
        }
    }
    state.active_runs.lock().await.remove(&run_id);
}

async fn run_inner(state: SharedState, run_id: Uuid, cancelled: Arc<AtomicBool>) -> Result<(), String> {
    let database = state.database.clone();
    let (project_id, task_id, project_name, project_context, task_title, task_brief, task_config): (Uuid, Uuid, String, String, String, String, Value) = sqlx::query_as(
        "SELECT p.id, t.id, p.name, p.description, t.title, t.description, t.config FROM runs r JOIN tasks t ON t.id = r.task_id JOIN projects p ON p.id = r.project_id WHERE r.id = $1",
    )
    .bind(run_id)
    .fetch_one(&database)
    .await
    .map_err(|error| format!("run not found: {error}"))?;
    // "Just ask" tasks: work out what is being asked (a position or research, day trade or swing) before anything else.
    let task_config = auto_route(&state, (project_id, task_id, run_id), &task_title, &task_brief, task_config).await;
    let mut settings = RunSettings::load(&database, &task_config).await;
    // Day trading needs minutes, not tens of minutes: one pass per agent and no mid-run hiring.
    let daytrade = task_config["style"].as_str() == Some("daytrade");
    if daytrade {
        settings.max_iterations = 1;
        settings.allow_agent_hiring = false;
        settings.max_rounds = 0;
    }
    let fast = daytrade && settings.depth == "quick";
    // The fast path gets its facts precomputed by the sidecar; long deliberation only delays an intraday call.
    if fast && settings.effort.is_none() {
        settings.effort = Some("low".into());
    }
    sqlx::query("UPDATE runs SET provider = $2, model = $3, config = $4 WHERE id = $1")
        .bind(run_id)
        .bind(settings.provider.as_str())
        .bind(&settings.model)
        .bind(json!(settings))
        .execute(&database)
        .await
        .map_err(|error| error.to_string())?;

    let ctx = Arc::new(RunContext {
        state: state.clone(),
        run_id,
        project_id,
        task_id,
        project_name,
        project_context,
        task_title,
        task_brief,
        settings,
        decision: RwLock::new(String::new()),
        intent: RwLock::new(String::new()),
        cancelled,
        trading: RwLock::new(None),
    });

    // The orchestrator is the only agent at birth.
    let orchestrator = insert_agent(&ctx, &AgentSpec {
        key: ORCHESTRATOR.into(),
        name: "Orchestrator".into(),
        role: "Chief of staff: hires, wires and supervises the research team".into(),
        kind: "orchestrator".into(),
        objective: format!("Deliver a decision-ready strategy for: {}", ctx.task_title),
        skills: vec![],
        depends_on: vec![],
        reports_to: None,
        max_iterations: Some(1),
    }, 1, None).await?;
    set_status(&ctx, orchestrator.id, "running", None).await;
    let route_summary = router::ROLES.iter().map(|role| format!("{role}={}", router::resolve(&ctx.settings, role)[0].label())).collect::<Vec<_>>().join(", ");
    ctx.event("run_started", format!("Orchestrator online. Model routes: {route_summary}."))
        .from(ORCHESTRATOR)
        .data(json!({"settings": ctx.settings}))
        .publish(&state)
        .await;

    // Long-term memory: what the firm already knows about this project.
    let memory_block = {
        let hits = memory::recall(&state.harness, project_id, &format!("{} {}", ctx.task_title, ctx.task_brief), 16).await;
        if !hits.is_empty() {
            ctx.event("memory_recalled", format!("Recalled {} memories from earlier research in this project.", hits.len())).from(ORCHESTRATOR).data(json!({"count": hits.len()})).publish(&state).await;
        }
        memory::render_context(&hits, MEMORY_PLAN_TOKENS)
    };

    // Trading desk mode: live market data pack + chart screenshots, loaded before the team is hired.
    let desk_brief = prepare_trading(&ctx, &task_config).await;

    if fast {
        // Fast path: no planning call. One head trader gets the whole data pack and the charts and answers directly.
        let decision = format!("Day trade decision now: long, short or wait — {}", ctx.task_title);
        *ctx.decision.write().await = decision.clone();
        *ctx.intent.write().await = "Goal: an immediately executable intraday decision using the desk rules in the data pack.\nSuccess criteria:\n- direction or WAIT\n- the line, the trigger candle condition, entry, fixed stop and targets\n- the session window and the cancel condition".to_string();
        sqlx::query("UPDATE runs SET intent = $2, status = 'running' WHERE id = $1").bind(run_id).bind(json!({"decision_to_make": decision, "rationale": "Fast day-trade path: single head trader, no planning step."})).execute(&database).await.map_err(|error| error.to_string())?;
        ctx.event("orchestrator_decision", "Fast day-trade path: one head trader, no planning step.").from(ORCHESTRATOR).publish(&state).await;
        let specs = validate_team(&ctx, vec![AgentSpec {
            key: "head_trader".into(),
            name: "Head Trader".into(),
            role: "Reads the charts, the session story and the body-close lines; decides long, short or wait".into(),
            kind: "strategist".into(),
            objective: "Decide now, top-down. First state what H4, then H1, then M15/M5 are doing (consolidation box with its body top and bottom, or impulse with direction), each proven by specific candle closes or wicks. If the higher timeframes consolidate and price is inside the box, give a RANGE plan: sell zone at the top edge and buy zone at the bottom edge, each with stop, targets and the M15/M5 trigger. If a body has closed outside the box, give the break → retest plan. Otherwise long, short or WAIT with the line, trigger, entry, fixed stop and targets. Always say what must print for price to continue up and to continue down, the session window and the cancel condition. Then give the decision table: when to sell, when to buy, when to hold, each with its reason, plus the fakeout case, the news inside the horizon and the risks. If the user wrote their own prediction, test it against the closes. Be brief: at most 350 words before the JSON block.".into(),
            skills: vec!["dow-structure-body-close".into(), "head-trader-trade-plan".into(), "chart-reading-technical".into()],
            depends_on: vec![],
            reports_to: Some(ORCHESTRATOR.into()),
            max_iterations: Some(1),
        }], &[ORCHESTRATOR.to_string()].into_iter().collect()).await;
        hire_all(&ctx, &specs, 1, ORCHESTRATOR).await?;
    } else {
        // Hiring: the orchestrator plans the team against the skill catalog.
        ctx.event("orchestrator_planning", "Analysing the request and designing the team…").from(ORCHESTRATOR).publish(&state).await;
        // Only a shortlist of the library reaches the prompt, retrieved for this task by the skill index.
        let catalog = state.harness.skills.catalog_for(&format!("{} {} {} {desk_brief}", ctx.project_name, ctx.task_title, ctx.task_brief), 40).await;
        let plan_request = LlmRequest {
            provider: ctx.settings.provider,
            model: None,
            system: prompts::SYSTEM_CONSTITUTION.into(),
            prompt: format!("{}{desk_brief}", prompts::plan_prompt(&ctx.project_name, &ctx.project_context, &ctx.task_title, &ctx.task_brief, if memory_block.is_empty() { "(nothing yet)" } else { &memory_block }, &catalog, ctx.settings.max_agents, &ctx.settings.depth)),
            web: false,
            timeout: Duration::from_secs(300),
            json_schema: Some(prompts::plan_schema()),
            progress: Some(progress_forwarder(&ctx, ORCHESTRATOR)),
            attachments: ctx.trading.read().await.as_ref().map(|trading| trading.attachments.clone()).unwrap_or_default(),
            effort: None,
        };
        let (plan_response, _) = state.harness.llm_routed(&plan_request, &router::resolve(&ctx.settings, "orchestrator"), ctx.site(Some(orchestrator.id), true)).await.map_err(|error| format!("orchestrator planning failed: {error}"))?;
        let plan = plan_response.structured.or_else(|| extract_json(&plan_response.text)).ok_or("orchestrator returned no plan")?;
        let decision = plan["decision_to_make"].as_str().unwrap_or(&ctx.task_title).to_string();
        *ctx.decision.write().await = decision.clone();
        let intent_text = render_intent(&plan);
        *ctx.intent.write().await = intent_text.clone();
        sqlx::query("UPDATE runs SET intent = $2, status = 'running' WHERE id = $1").bind(run_id).bind(json!({"decision_to_make": decision, "intent": plan["intent"], "rationale": plan["rationale"]})).execute(&database).await.map_err(|error| error.to_string())?;
        sqlx::query("UPDATE run_agents SET output = $2, summary = $3 WHERE id = $1").bind(orchestrator.id).bind(&intent_text).bind(plan["rationale"].as_str()).execute(&database).await.map_err(|error| error.to_string())?;
        ctx.event("orchestrator_decision", format!("Decision to make: {decision}")).from(ORCHESTRATOR).data(json!({"intent": plan["intent"], "rationale": plan["rationale"]})).publish(&state).await;

        let specs: Vec<AgentSpec> = serde_json::from_value(plan["agents"].clone()).map_err(|error| format!("plan has invalid agents: {error}"))?;
        let specs = validate_team(&ctx, specs, &HashSet::new()).await;
        if specs.is_empty() {
            return Err("orchestrator hired no agents".into());
        }
        hire_all(&ctx, &specs, 1, ORCHESTRATOR).await?;
    }

    // Rounds: the critic can send the team back for one more pass.
    let mut round = 1;
    loop {
        schedule(&ctx).await?;
        if ctx.cancelled.load(Ordering::SeqCst) {
            return Err("cancelled by user".into());
        }
        if round > ctx.settings.max_rounds as usize {
            break;
        }
        let Some(critic) = latest_of_kind(&ctx, "critic").await else { break };
        let report = critic.output.as_deref().and_then(extract_json).unwrap_or(Value::Null);
        let verdict = report["verdict"].as_str().unwrap_or_default().to_ascii_lowercase();
        if !(verdict.contains("revise") || verdict.contains("another")) {
            break;
        }
        let follow_ups: Vec<AgentSpec> = serde_json::from_value(report["hire"].clone()).unwrap_or_default();
        if follow_ups.is_empty() {
            break;
        }
        round += 1;
        sqlx::query("UPDATE runs SET round = $2 WHERE id = $1").bind(run_id).bind(round as i32).execute(&database).await.map_err(|error| error.to_string())?;
        ctx.event("round_started", format!("Critic requested revisions. Round {round}: hiring {} follow-up specialists.", follow_ups.len().min(3))).from(&critic.key).to(ORCHESTRATOR).data(json!({"issues": report["issues"]})).publish(&state).await;
        let existing = existing_keys(&ctx).await;
        let mut follow_ups: Vec<AgentSpec> = follow_ups.into_iter().take(3).map(|mut spec| {
            spec.key = format!("{}_r{round}", super::skills::slugify(&spec.key).replace('-', "_"));
            spec.kind = "researcher".into();
            spec.depends_on.retain(|key| existing.contains(key));
            if spec.depends_on.is_empty() { spec.depends_on.push("intent_analyst".into()) }
            spec.reports_to = Some(critic.key.clone());
            spec
        }).collect();
        let previous_strategist = latest_of_kind(&ctx, "strategist").await.map(|agent| agent.key).unwrap_or_default();
        let follow_keys: Vec<String> = follow_ups.iter().map(|spec| spec.key.clone()).collect();
        follow_ups.push(AgentSpec {
            key: format!("strategist_r{round}"),
            name: format!("Chief Strategist (round {round})"),
            role: "Revises the decision with the critic's issues and new evidence".into(),
            kind: "strategist".into(),
            objective: "Revise the recommendation: address every critic issue, integrate follow-up findings, and restate the decision with updated confidence.".into(),
            skills: vec!["decision-strategy".into(), "synthesis-report-writing".into()],
            depends_on: [vec![previous_strategist, critic.key.clone()], follow_keys].concat(),
            reports_to: Some(ORCHESTRATOR.into()),
            max_iterations: Some(1),
        });
        let follow_ups = validate_team(&ctx, follow_ups, &existing).await;
        hire_all(&ctx, &follow_ups, round as i32, &critic.key).await?;
    }

    finalize(&ctx, orchestrator.id).await
}

fn render_intent(plan: &Value) -> String {
    let intent = &plan["intent"];
    let list = |value: &Value| value.as_array().map(|items| items.iter().filter_map(Value::as_str).map(|item| format!("- {item}")).collect::<Vec<_>>().join("\n")).unwrap_or_default();
    format!(
        "Goal: {}\nAudience: {}\nSuccess criteria:\n{}\nAssumptions:\n{}\nUnknowns:\n{}",
        intent["goal"].as_str().unwrap_or(""),
        intent["audience"].as_str().unwrap_or("the user"),
        list(&intent["success_criteria"]),
        list(&intent["assumptions"]),
        list(&intent["unknowns"])
    )
}

/// Paperclip-style hire validation: unique keys, dependencies exist, acyclic (Kahn), skills exist, team size cap,
/// mandatory intent → … → strategist → critic shape on the first round.
async fn validate_team(ctx: &RunContext, specs: Vec<AgentSpec>, existing: &HashSet<String>) -> Vec<AgentSpec> {
    let harness = &ctx.state.harness;
    let mut seen: HashSet<String> = existing.clone();
    seen.insert(ORCHESTRATOR.into());
    let mut team: Vec<AgentSpec> = Vec::new();
    for mut spec in specs {
        spec.key = super::skills::slugify(&spec.key).replace('-', "_");
        if spec.key.is_empty() || seen.contains(&spec.key) || spec.objective.trim().is_empty() {
            continue;
        }
        spec.depends_on.retain(|dependency| seen.contains(dependency) && dependency != ORCHESTRATOR);
        spec.depends_on.dedup();
        if spec.role.trim().is_empty() {
            spec.role = spec.kind.clone();
        }
        seen.insert(spec.key.clone());
        team.push(spec);
    }
    if existing.is_empty() {
        let intent_key = team.iter().find(|spec| spec.kind == "intent").map(|spec| spec.key.clone());
        if let Some(intent_key) = intent_key {
            for spec in team.iter_mut().filter(|spec| spec.kind != "intent" && spec.depends_on.is_empty()) {
                spec.depends_on.push(intent_key.clone());
            }
        }
        let limit = ctx.settings.max_agents;
        if team.len() > limit {
            // Keep the decision spine; trim researchers/analysts from the end.
            let spine: HashSet<&str> = ["intent", "strategist", "critic"].into();
            let mut kept = Vec::new();
            let mut extra = limit.saturating_sub(team.iter().filter(|spec| spine.contains(spec.kind.as_str())).count());
            for spec in team {
                if spine.contains(spec.kind.as_str()) {
                    kept.push(spec);
                } else if extra > 0 {
                    extra -= 1;
                    kept.push(spec);
                }
            }
            let kept_keys: HashSet<String> = kept.iter().map(|spec| spec.key.clone()).collect();
            for spec in kept.iter_mut() {
                spec.depends_on.retain(|dependency| kept_keys.contains(dependency) || existing.contains(dependency));
            }
            team = kept;
        }
        if !team.iter().any(|spec| spec.kind == "strategist") {
            let leaves: Vec<String> = team.iter().map(|spec| spec.key.clone()).collect();
            team.push(AgentSpec { key: "strategist".into(), name: "Chief Strategist".into(), role: "Decision synthesis".into(), kind: "strategist".into(), objective: "Synthesize all reports into a scored, decision-ready recommendation with next actions.".into(), skills: vec!["decision-strategy".into()], depends_on: leaves, reports_to: Some(ORCHESTRATOR.into()), max_iterations: Some(1) });
        }
        if !team.iter().any(|spec| spec.kind == "critic") && ctx.settings.depth != "quick" {
            let strategist = team.iter().find(|spec| spec.kind == "strategist").map(|spec| spec.key.clone()).unwrap_or_default();
            team.push(AgentSpec { key: "critic".into(), name: "Red Team Critic".into(), role: "Adversarial review".into(), kind: "critic".into(), objective: "Challenge the recommendation, verify decision-critical claims, and decide accept or revise.".into(), skills: vec!["critic-red-team".into()], depends_on: vec![strategist], reports_to: Some(ORCHESTRATOR.into()), max_iterations: Some(1) });
        }
    }
    if !is_acyclic(&team, existing) {
        // A cycle means the model referenced later agents; fall back to depending on everything hired before.
        let mut before: Vec<String> = Vec::new();
        for spec in team.iter_mut() {
            spec.depends_on.retain(|dependency| before.contains(dependency) || existing.contains(dependency));
            before.push(spec.key.clone());
        }
    }
    // Skill resolution: keep the orchestrator's valid picks; agents hired without (valid) skills get index matches
    // for their own role and objective. A lookup in the skill index, not a model call.
    for spec in team.iter_mut() {
        let picked = harness.skills.resolve(&spec.skills, "", "", 3).await;
        spec.skills = if picked.is_empty() { harness.skills.resolve(&[], &format!("{} {}", spec.name, spec.role), &spec.objective, 2).await } else { picked };
    }
    team
}

fn is_acyclic(team: &[AgentSpec], existing: &HashSet<String>) -> bool {
    let keys: HashSet<&str> = team.iter().map(|spec| spec.key.as_str()).collect();
    let mut indegree: HashMap<&str, usize> = team.iter().map(|spec| (spec.key.as_str(), spec.depends_on.iter().filter(|dependency| keys.contains(dependency.as_str())).count())).collect();
    let mut queue: VecDeque<&str> = indegree.iter().filter(|(_, degree)| **degree == 0).map(|(key, _)| *key).collect();
    let mut visited = 0;
    while let Some(key) = queue.pop_front() {
        visited += 1;
        for spec in team.iter().filter(|spec| spec.depends_on.iter().any(|dependency| dependency == key)) {
            let degree = indegree.get_mut(spec.key.as_str()).unwrap();
            *degree -= 1;
            if *degree == 0 {
                queue.push_back(spec.key.as_str());
            }
        }
    }
    let _ = existing;
    visited == team.len()
}

async fn hire_all(ctx: &RunContext, specs: &[AgentSpec], round: i32, hired_by: &str) -> Result<(), String> {
    for spec in specs {
        let agent = insert_agent(ctx, spec, round, Some(hired_by)).await?;
        ctx.event("agent_hired", format!("Hired {} — {}", agent.name, agent.role))
            .from(hired_by)
            .to(&agent.key)
            .data(json!({"kind": agent.kind, "skills": agent.skills, "depends_on": agent.depends_on, "model": format!("{}:{}", agent.provider, agent.model.clone().unwrap_or_default()), "objective": agent.objective}))
            .publish(&ctx.state)
            .await;
        tokio::time::sleep(Duration::from_millis(120)).await;
    }
    Ok(())
}

async fn insert_agent(ctx: &RunContext, spec: &AgentSpec, round: i32, hired_by: Option<&str>) -> Result<RunAgent, String> {
    let route = router::resolve(&ctx.settings, router::role_for_kind(&spec.kind)).remove(0);
    let max_iterations = spec.max_iterations.unwrap_or(ctx.settings.max_iterations).clamp(1, ctx.settings.max_iterations.max(1));
    sqlx::query_as::<_, RunAgent>(&format!(
        "INSERT INTO run_agents (id, run_id, task_id, key, name, role, kind, objective, reports_to, hired_by, skills, depends_on, round, provider, model, max_iterations)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16) RETURNING {AGENT_COLUMNS}"
    ))
    .bind(Uuid::new_v4())
    .bind(ctx.run_id)
    .bind(ctx.task_id)
    .bind(&spec.key)
    .bind(spec.name.trim())
    .bind(spec.role.trim())
    .bind(&spec.kind)
    .bind(spec.objective.trim())
    .bind(spec.reports_to.clone().or_else(|| hired_by.map(String::from)))
    .bind(hired_by)
    .bind(&spec.skills)
    .bind(&spec.depends_on)
    .bind(round)
    .bind(route.provider.as_str())
    .bind(&route.model)
    .bind(max_iterations)
    .fetch_one(&ctx.state.database)
    .await
    .map_err(|error| format!("could not hire {}: {error}", spec.key))
}

async fn load_agents(ctx: &RunContext) -> Vec<RunAgent> {
    sqlx::query_as::<_, RunAgent>(&format!("SELECT {AGENT_COLUMNS} FROM run_agents WHERE run_id = $1 ORDER BY created_at"))
        .bind(ctx.run_id)
        .fetch_all(&ctx.state.database)
        .await
        .unwrap_or_default()
}

async fn existing_keys(ctx: &RunContext) -> HashSet<String> {
    load_agents(ctx).await.into_iter().map(|agent| agent.key).collect()
}

async fn latest_of_kind(ctx: &RunContext, kind: &str) -> Option<RunAgent> {
    load_agents(ctx).await.into_iter().filter(|agent| agent.kind == kind && agent.status == "complete").max_by_key(|agent| (agent.round, agent.created_at))
}

async fn set_status(ctx: &RunContext, agent_id: Uuid, status: &str, error: Option<&str>) {
    let _ = sqlx::query(
        "UPDATE run_agents SET status = $2, error = CASE WHEN $2 = 'complete' THEN NULL ELSE COALESCE($3, error) END,
         started_at = CASE WHEN $2 = 'running' AND started_at IS NULL THEN NOW() ELSE started_at END,
         completed_at = CASE WHEN $2 IN ('complete','failed','skipped') THEN NOW() ELSE completed_at END
         WHERE id = $1",
    )
    .bind(agent_id)
    .bind(status)
    .bind(error)
    .execute(&ctx.state.database)
    .await;
}

fn terminal(status: &str) -> bool {
    matches!(status, "complete" | "failed" | "skipped")
}

/// Concurrency-limited DAG scheduler (Symphony tick loop): dispatch ready nodes while slots remain, retry failures
/// with backoff, and skip nodes whose every dependency failed.
async fn schedule(ctx: &Arc<RunContext>) -> Result<(), String> {
    let mut running: JoinSet<(Uuid, Result<(), String>)> = JoinSet::new();
    let mut in_flight: HashSet<Uuid> = HashSet::new();
    loop {
        if ctx.cancelled.load(Ordering::SeqCst) {
            running.abort_all();
            let _ = sqlx::query("UPDATE run_agents SET status = 'skipped', error = 'cancelled' WHERE run_id = $1 AND status NOT IN ('complete','failed','skipped')").bind(ctx.run_id).execute(&ctx.state.database).await;
            return Ok(());
        }
        let agents: Vec<RunAgent> = load_agents(ctx).await.into_iter().filter(|agent| agent.key != ORCHESTRATOR).collect();
        let by_key: HashMap<&str, &RunAgent> = agents.iter().map(|agent| (agent.key.as_str(), agent)).collect();

        let mut ready = Vec::new();
        for agent in agents.iter().filter(|agent| !in_flight.contains(&agent.id)) {
            let waiting = match agent.status.as_str() {
                "pending" => true,
                "retrying" => agent.next_retry_at.map(|at| at <= Utc::now()).unwrap_or(true),
                _ => false,
            };
            if !waiting {
                continue;
            }
            let dependencies: Vec<&&RunAgent> = agent.depends_on.iter().filter_map(|key| by_key.get(key.as_str())).collect();
            if dependencies.iter().any(|dependency| !terminal(&dependency.status)) {
                continue;
            }
            if !dependencies.is_empty() && dependencies.iter().all(|dependency| dependency.status != "complete") {
                set_status(ctx, agent.id, "skipped", Some("all dependencies failed")).await;
                ctx.event("agent_skipped", format!("{} skipped: every dependency failed.", agent.name)).from(&agent.key).publish(&ctx.state).await;
                continue;
            }
            ready.push(agent.clone());
        }

        let slots = ctx.settings.concurrency.saturating_sub(in_flight.len());
        for agent in ready.into_iter().take(slots) {
            in_flight.insert(agent.id);
            let id = agent.id;
            let timeout = Duration::from_secs(ctx.settings.agent_timeout_secs * agent.max_iterations.max(1) as u64);
            let agent_ctx = ctx.clone();
            running.spawn(async move {
                let result = match tokio::time::timeout(timeout, run_agent(&agent_ctx, agent)).await {
                    Ok(result) => result,
                    Err(_) => Err(format!("timed out after {}s", timeout.as_secs())),
                };
                (id, result)
            });
        }

        let all_done = agents.iter().all(|agent| terminal(&agent.status)) && in_flight.is_empty();
        if all_done {
            return Ok(());
        }

        tokio::select! {
            joined = running.join_next(), if !running.is_empty() => {
                if let Some(Ok((agent_id, result))) = joined {
                    in_flight.remove(&agent_id);
                    if let Err(error) = result {
                        handle_failure(ctx, agent_id, &error).await;
                    }
                }
            }
            _ = tokio::time::sleep(Duration::from_millis(800)) => {}
        }
    }
}

async fn handle_failure(ctx: &RunContext, agent_id: Uuid, error: &str) {
    let Some(agent) = load_agents(ctx).await.into_iter().find(|agent| agent.id == agent_id) else { return };
    if agent.attempt < ctx.settings.max_attempts {
        let delay = retry_delay(agent.attempt.max(1) as u32, DelayType::Failure, MAX_RETRY_BACKOFF_MS);
        let due = Utc::now() + chrono::Duration::from_std(delay).unwrap_or_default();
        let _ = sqlx::query("UPDATE run_agents SET status = 'retrying', error = $2, next_retry_at = $3 WHERE id = $1").bind(agent_id).bind(error).bind(due).execute(&ctx.state.database).await;
        ctx.event("agent_retry", format!("{} failed (attempt {}/{}), retrying in {}s: {}", agent.name, agent.attempt, ctx.settings.max_attempts, delay.as_secs(), truncate_chars(error, 200)))
            .from(&agent.key)
            .data(json!({"attempt": agent.attempt, "delay_secs": delay.as_secs()}))
            .publish(&ctx.state)
            .await;
    } else {
        set_status(ctx, agent_id, "failed", Some(error)).await;
        ctx.event("agent_error", format!("{} failed after {} attempts: {}", agent.name, agent.attempt, truncate_chars(error, 300)))
            .from(&agent.key)
            .to(agent.reports_to.as_deref().unwrap_or(ORCHESTRATOR))
            .publish(&ctx.state)
            .await;
    }
}

/// Compact dependency report: summary + top findings + numbers + risks + open questions, capped per dependency.
fn compact_report(agent: &RunAgent, max_chars: usize) -> String {
    let report = agent.output.as_deref().and_then(extract_json).unwrap_or(Value::Null);
    let mut compact = json!({
        "summary": agent.summary.clone().unwrap_or_default(),
        "key_findings": report["key_findings"].as_array().map(|items| items.iter().take(6).cloned().collect::<Vec<_>>()).unwrap_or_default(),
        "numbers": report["numbers"],
        "risks": report["risks"],
        "open_questions": report["open_questions"],
    });
    if let Some(decision) = report.get("decision") {
        compact["decision"] = decision.clone();
    }
    if let Some(verdict) = report.get("verdict") {
        compact["verdict"] = verdict.clone();
        compact["issues"] = report["issues"].clone();
    }
    let mut text = serde_json::to_string(&compact).unwrap_or_default();
    if text.len() > max_chars {
        // Drop evidence detail first, then lower-confidence findings (Paperclip guidance).
        if let Some(findings) = compact["key_findings"].as_array_mut() {
            for finding in findings.iter_mut() {
                if let Some(object) = finding.as_object_mut() {
                    object.remove("evidence");
                }
            }
            findings.sort_by(|a, b| b["confidence"].as_f64().unwrap_or(0.0).partial_cmp(&a["confidence"].as_f64().unwrap_or(0.0)).unwrap_or(std::cmp::Ordering::Equal));
            findings.truncate(4);
        }
        text = truncate_chars(&serde_json::to_string(&compact).unwrap_or_default(), max_chars);
    }
    format!("<dependency key=\"{}\" name=\"{}\" role=\"{}\">\n{}\n</dependency>", agent.key, agent.name, agent.role, text)
}

/// One agent's loop: act (LLM with web) → observe (parse report) → decide (continue / done), bounded by
/// iterations. Providers without native browsing get an evidence pack from the harness.
async fn run_agent(ctx: &RunContext, agent: RunAgent) -> Result<(), String> {
    let harness = &ctx.state.harness;
    let database = &ctx.state.database;
    sqlx::query("UPDATE run_agents SET attempt = attempt + 1, status = 'running', started_at = COALESCE(started_at, NOW()) WHERE id = $1").bind(agent.id).execute(database).await.map_err(|error| error.to_string())?;
    ctx.event("agent_started", format!("{} started: {}", agent.name, truncate_chars(&agent.objective, 160)))
        .from(agent.reports_to.as_deref().unwrap_or(ORCHESTRATOR))
        .to(&agent.key)
        .data(json!({"attempt": agent.attempt + 1, "skills": agent.skills}))
        .publish(&ctx.state)
        .await;

    // Data exchange along graph edges.
    let all = load_agents(ctx).await;
    let dependencies: Vec<&RunAgent> = agent.depends_on.iter().filter_map(|key| all.iter().find(|candidate| &candidate.key == key && candidate.status == "complete")).collect();
    let per_dependency = if dependencies.len() > 4 { DEPENDENCY_CHARS * 4 / dependencies.len() } else { DEPENDENCY_CHARS };
    let mut dependency_block = Vec::new();
    for dependency in &dependencies {
        dependency_block.push(compact_report(dependency, per_dependency.max(1200)));
        ctx.event("agent_handoff", format!("{} → {}: {}", dependency.name, agent.name, truncate_chars(dependency.summary.as_deref().unwrap_or("report"), 180)))
            .from(&dependency.key)
            .to(&agent.key)
            .publish(&ctx.state)
            .await;
    }
    let dependency_block = dependency_block.join("\n\n");

    let (skills_block, _) = harness.skills.render(&agent.skills).await;
    let trading = ctx.trading.read().await.clone();
    let market_block = trading.as_ref().map(|trading| trading.market.clone()).unwrap_or_default();
    // Chart screenshots go to the chart readers and the head trader; if nobody reads charts, the intent agent does.
    let chart_readers = all.iter().any(|candidate| candidate.skills.iter().any(|skill| skill == "chart-reading-technical") || candidate.key.contains("chart"));
    let reads_charts = agent.skills.iter().any(|skill| skill == "chart-reading-technical") || agent.key.contains("chart") || agent.kind == "strategist" || (!chart_readers && agent.kind == "intent");
    let attachments = match &trading { Some(trading) if reads_charts => trading.attachments.clone(), _ => vec![] };
    let memory_block = if agent.kind != "critic" {
        memory::render_context(&memory::recall(harness, ctx.project_id, &agent.objective, 8).await, MEMORY_AGENT_TOKENS)
    } else {
        String::new()
    };
    let routes = router::resolve(&ctx.settings, router::role_for_kind(&agent.kind));
    let native_web = routes[0].provider.native_web();
    let mut previous = String::new();
    let mut open_questions = String::new();
    let mut final_text = String::new();
    let mut final_report = Value::Null;

    for iteration in 1..=agent.max_iterations {
        if ctx.cancelled.load(Ordering::SeqCst) {
            return Err("cancelled".into());
        }
        let replay = trading.as_ref().is_some_and(|trading| trading.as_of.is_some());
        let evidence = if native_web || replay || agent.kind == "strategist" { String::new() } else { gather_evidence(ctx, &agent, &open_questions).await };
        let intent = ctx.intent.read().await.clone();
        let decision = ctx.decision.read().await.clone();
        let prompt = prompts::agent_prompt(&AgentBrief {
            project: &ctx.project_name,
            task_title: &ctx.task_title,
            task_brief: &ctx.task_brief,
            decision: &decision,
            intent: &intent,
            skills: &skills_block,
            memory: &memory_block,
            market: &market_block,
            trading: trading.is_some(),
            dependencies: &dependency_block,
            evidence: &evidence,
            name: &agent.name,
            role: &agent.role,
            kind: &agent.kind,
            objective: &agent.objective,
            iteration,
            max_iterations: agent.max_iterations,
            previous: &previous,
        });
        let request = LlmRequest {
            provider: routes[0].provider,
            model: routes[0].model.clone(),
            system: prompts::SYSTEM_CONSTITUTION.into(),
            prompt,
            // Replay runs never browse: the web knows what happened next.
            web: native_web && agent.kind != "strategist" && !trading.as_ref().is_some_and(|trading| trading.as_of.is_some()),
            timeout: Duration::from_secs(ctx.settings.agent_timeout_secs),
            json_schema: None,
            progress: Some(progress_forwarder(ctx, &agent.key)),
            attachments: attachments.clone(),
            effort: ctx.settings.effort.clone(),
        };
        ctx.event("agent_iteration", format!("{} · iteration {iteration}/{}{}", agent.name, agent.max_iterations, if request.web { " · browsing the web" } else { "" }))
            .from(&agent.key)
            .data(json!({"iteration": iteration, "prompt_tokens": estimate_tokens(&request.prompt)}))
            .publish(&ctx.state)
            .await;
        let (response, used_route) = match harness.llm_routed(&request, &routes, ctx.site(Some(agent.id), !request.web)).await {
            Ok(result) => result,
            // Keep what earlier iterations produced when a later one hits a usage limit with no fallback left.
            Err(error) if error.class == super::retry::ErrorClass::Quota && !final_text.is_empty() => {
                ctx.event("agent_limit", format!("{}: provider usage limit reached; keeping iteration {} results.", agent.name, iteration - 1)).from(&agent.key).publish(&ctx.state).await;
                break;
            }
            Err(error) => return Err(error.to_string()),
        };
        if used_route != routes[0] {
            let _ = sqlx::query("UPDATE run_agents SET provider = $2, model = $3 WHERE id = $1").bind(agent.id).bind(used_route.provider.as_str()).bind(&used_route.model).execute(database).await;
            ctx.event("route_fallback", format!("{} switched to {} (primary {} unavailable).", agent.name, used_route.label(), routes[0].label())).from(&agent.key).publish(&ctx.state).await;
        }
        let report = extract_json(&response.text).unwrap_or_else(|| json!({"status": "done", "summary": truncate_chars(&response.text, 600)}));
        final_text = response.text.clone();
        final_report = report.clone();
        let summary = report["summary"].as_str().map(String::from).unwrap_or_else(|| truncate_chars(&response.text, 600));
        let sources = report["sources"].clone();
        sqlx::query("UPDATE run_agents SET output = $2, summary = $3, sources = $4, iterations = $5 WHERE id = $1")
            .bind(agent.id)
            .bind(&final_text)
            .bind(&summary)
            .bind(if sources.is_array() { sources } else { json!([]) })
            .bind(iteration)
            .execute(database)
            .await
            .map_err(|error| error.to_string())?;
        ctx.event("agent_progress", format!("{}: {}", agent.name, truncate_chars(&summary, 220)))
            .from(&agent.key)
            .data(json!({"iteration": iteration, "tokens_in": response.usage.input, "tokens_out": response.usage.output, "cache_read": response.usage.cache_read, "cache_hit": response.cache_hit, "web_searches": response.usage.web_searches}))
            .publish(&ctx.state)
            .await;
        let wants_more = report["status"].as_str() == Some("continue");
        if !wants_more || iteration >= agent.max_iterations {
            break;
        }
        // Compaction for the next iteration: the model gets a query-focused digest, not the whole transcript.
        open_questions = report["open_questions"].as_array().map(|items| items.iter().filter_map(Value::as_str).collect::<Vec<_>>().join("; ")).unwrap_or_default();
        let digest_query = format!("{} {}", agent.objective, open_questions);
        let raw_tokens = estimate_tokens(&final_text);
        previous = compress_text(&final_text, &digest_query, 900);
        ledger::record_savings(database, ctx.run_id, raw_tokens, estimate_tokens(&previous)).await;
    }

    if agent.kind == "intent" {
        let summary = final_report["summary"].as_str().unwrap_or_default();
        if !summary.is_empty() {
            let mut intent = ctx.intent.write().await;
            intent.push_str(&format!("\nIntent analyst refinement: {summary}"));
        }
    }
    set_status(ctx, agent.id, "complete", None).await;
    ctx.event("agent_result", format!("{} reported to {}: {}", agent.name, agent.reports_to.as_deref().unwrap_or(ORCHESTRATOR), truncate_chars(final_report["summary"].as_str().unwrap_or("done"), 200)))
        .from(&agent.key)
        .to(agent.reports_to.as_deref().unwrap_or(ORCHESTRATOR))
        .publish(&ctx.state)
        .await;

    retain_findings(ctx, &agent, &final_report).await;
    // Framing and review agents do not hire: the intent agent frames the work, the critic uses rounds instead.
    if ctx.settings.allow_agent_hiring && !matches!(agent.kind.as_str(), "critic" | "intent") {
        hire_requested(ctx, &agent, &final_report).await;
    }
    Ok(())
}

/// Turns provider progress into live `agent_activity` events so the UI shows what each agent is doing right now:
/// every web search, page read and result, plus throttled reasoning/writing snippets.
fn progress_forwarder(ctx: &RunContext, agent_key: &str) -> ProgressSink {
    let (sender, mut receiver) = tokio::sync::mpsc::unbounded_channel::<Progress>();
    let state = ctx.state.clone();
    let (project_id, task_id, run_id, key) = (ctx.project_id, ctx.task_id, ctx.run_id, agent_key.to_string());
    tokio::spawn(async move {
        let mut last_text = std::time::Instant::now() - Duration::from_secs(10);
        let (mut searches, mut pages) = (0u32, 0u32);
        while let Some(item) = receiver.recv().await {
            let (activity, message) = match item {
                Progress::Search(query) => {
                    searches += 1;
                    ("search", format!("searching: {query}"))
                }
                Progress::Fetch(url) => {
                    pages += 1;
                    ("fetch", format!("reading {url}"))
                }
                Progress::Result(summary) => ("result", format!("got {summary}")),
                Progress::Tool(name) => ("tool", format!("using {name}")),
                Progress::Thinking(text) | Progress::Writing(text) if last_text.elapsed() < Duration::from_secs(3) => {
                    let _ = text;
                    continue;
                }
                Progress::Thinking(text) => {
                    last_text = std::time::Instant::now();
                    ("thinking", text)
                }
                Progress::Writing(text) => {
                    last_text = std::time::Instant::now();
                    ("writing", text)
                }
                Progress::RateLimit(info) => {
                    state.harness.note_limit("claude", info).await;
                    continue;
                }
            };
            EventDraft::new(project_id, task_id, run_id, "agent_activity", message)
                .from(&key)
                .data(json!({"activity": activity, "searches": searches, "pages": pages}))
                .publish(&state)
                .await;
        }
    });
    sender
}

/// Harness-side web research for providers without native browsing: queries → search → fetch → compress.
async fn gather_evidence(ctx: &RunContext, agent: &RunAgent, open_questions: &str) -> String {
    let harness = &ctx.state.harness;
    let request = LlmRequest {
        provider: ctx.settings.provider,
        model: None,
        system: prompts::SYSTEM_CONSTITUTION.into(),
        prompt: prompts::search_queries_prompt(&agent.objective, open_questions, &ctx.decision.read().await),
        web: false,
        timeout: Duration::from_secs(120),
        json_schema: Some(prompts::queries_schema()),
        progress: None,
        attachments: vec![],
        effort: None,
    };
    let queries: Vec<String> = match harness.llm_routed(&request, &router::resolve(&ctx.settings, "utility"), ctx.site(Some(agent.id), true)).await {
        Ok((response, _)) => response.structured.and_then(|value| serde_json::from_value(value["queries"].clone()).ok()).unwrap_or_default(),
        Err(_) => vec![],
    };
    let queries = if queries.is_empty() { vec![agent.objective.clone()] } else { queries };
    ctx.event("agent_searching", format!("{} searching: {}", agent.name, queries.join(" · "))).from(&agent.key).publish(&ctx.state).await;
    let mut hits = Vec::new();
    for query in queries.iter().take(4) {
        for hit in web::search(&ctx.state.database, &harness.http, query, 5).await {
            if !hits.iter().any(|existing: &web::SearchHit| existing.url == hit.url) {
                hits.push(hit);
            }
        }
    }
    let focus = format!("{} {}", agent.objective, open_questions);
    let mut blocks = Vec::new();
    let mut raw = 0;
    let mut compressed = 0;
    for (index, hit) in hits.iter().take(8).enumerate() {
        let mut block = format!("[{}] {} <{}>\n{}", index + 1, hit.title, hit.url, hit.snippet);
        if index < 3 {
            if let Some(text) = web::fetch_text(&ctx.state.database, &harness.http, &hit.url).await {
                raw += estimate_tokens(&text);
                let digest = compress_text(&text, &focus, 700);
                compressed += estimate_tokens(&digest);
                block.push_str(&format!("\n{digest}"));
            }
        }
        blocks.push(block);
    }
    ledger::record_savings(&ctx.state.database, ctx.run_id, raw, compressed).await;
    blocks.join("\n\n")
}

async fn retain_findings(ctx: &RunContext, agent: &RunAgent, report: &Value) {
    let mut facts: Vec<NewMemory> = report["key_findings"]
        .as_array()
        .map(|findings| {
            findings
                .iter()
                .filter_map(|finding| {
                    let claim = finding["claim"].as_str()?;
                    let confidence = finding["confidence"].as_f64().unwrap_or(0.6) as f32;
                    Some(NewMemory {
                        kind: "world".into(),
                        content: match finding["evidence"].as_str() { Some(evidence) if !evidence.is_empty() => format!("{claim} ({evidence})"), _ => claim.to_string() },
                        entities: vec![],
                        source_url: finding["source"].as_str().filter(|url| url.starts_with("http")).map(String::from),
                        importance: confidence,
                        confidence: Some(confidence),
                    })
                })
                .collect()
        })
        .unwrap_or_default();
    if let Some(recommendation) = report["decision"]["recommendation"].as_str() {
        facts.push(NewMemory { kind: "opinion".into(), content: format!("Recommendation for \"{}\": {recommendation}", ctx.task_title), entities: vec![], source_url: None, importance: 0.9, confidence: report["decision"]["confidence"].as_f64().map(|value| value as f32) });
    }
    if facts.is_empty() {
        return;
    }
    let scope = Scope { project_id: ctx.project_id, task_id: Some(ctx.task_id), run_id: Some(ctx.run_id), agent_key: Some(agent.key.clone()) };
    let report = memory::retain(&ctx.state.harness, &scope, facts).await;
    if report.added + report.reinforced > 0 {
        ctx.event("memory_retained", format!("{} stored {} new facts ({} reinforced).", agent.name, report.added, report.reinforced)).from(&agent.key).data(json!(report)).publish(&ctx.state).await;
    }
}

/// Mid-run hiring requested by an agent (Paperclip agent-hires): validated, depth-capped, wired into the decision.
async fn hire_requested(ctx: &RunContext, requester: &RunAgent, report: &Value) {
    let Ok(requests) = serde_json::from_value::<Vec<AgentSpec>>(report["hire"].clone()) else { return };
    if requests.is_empty() {
        return;
    }
    let all = load_agents(ctx).await;
    let depth = hire_depth(&all, &requester.key);
    let headroom = (ctx.settings.max_agents + 4).saturating_sub(all.len().saturating_sub(1));
    if depth >= MAX_HIRE_DEPTH || headroom == 0 {
        ctx.event("hire_denied", format!("{} asked to hire {} specialists; denied (depth {depth}, team full: {}).", requester.name, requests.len(), headroom == 0)).from(ORCHESTRATOR).to(&requester.key).publish(&ctx.state).await;
        return;
    }
    let existing: HashSet<String> = all.iter().map(|agent| agent.key.clone()).collect();
    let specs: Vec<AgentSpec> = requests.into_iter().take(2.min(headroom)).map(|mut spec| {
        spec.kind = if spec.kind == "strategist" || spec.kind == "critic" { "specialist".into() } else { spec.kind };
        spec.depends_on = vec![requester.key.clone()];
        spec.reports_to = Some(requester.key.clone());
        spec.max_iterations = Some(1);
        spec
    }).collect();
    let specs = validate_team(ctx, specs, &existing).await;
    if specs.is_empty() {
        return;
    }
    let keys: Vec<String> = specs.iter().map(|spec| spec.key.clone()).collect();
    if hire_all(ctx, &specs, requester.round, &requester.key).await.is_err() {
        return;
    }
    // Results of new hires must reach the decision: pending strategists of this round now depend on them too.
    let _ = sqlx::query("UPDATE run_agents SET depends_on = depends_on || $2 WHERE run_id = $1 AND kind = 'strategist' AND status = 'pending' AND round = $3")
        .bind(ctx.run_id)
        .bind(&keys)
        .bind(requester.round)
        .execute(&ctx.state.database)
        .await;
}

fn hire_depth(all: &[RunAgent], key: &str) -> usize {
    let by_key: BTreeMap<&str, &RunAgent> = all.iter().map(|agent| (agent.key.as_str(), agent)).collect();
    let mut depth = 0;
    let mut current = key;
    while let Some(agent) = by_key.get(current) {
        match agent.hired_by.as_deref() {
            Some(parent) if parent != ORCHESTRATOR && depth < 8 => {
                depth += 1;
                current = parent;
            }
            _ => break,
        }
    }
    depth
}

async fn finalize(ctx: &RunContext, orchestrator_id: Uuid) -> Result<(), String> {
    let database = &ctx.state.database;
    let agents = load_agents(ctx).await;
    let strategist = agents.iter().filter(|agent| agent.kind == "strategist" && agent.status == "complete").max_by_key(|agent| (agent.round, agent.created_at));
    let critic = agents.iter().filter(|agent| agent.kind == "critic" && agent.status == "complete").max_by_key(|agent| (agent.round, agent.created_at));
    let Some(strategist) = strategist else {
        return Err("no strategist completed, so no decision could be made".into());
    };
    let strategist_report = strategist.output.as_deref().and_then(extract_json).unwrap_or(Value::Null);
    let decision = strategist_report.get("decision").cloned().unwrap_or(Value::Null);
    let critic_report = critic.and_then(|agent| agent.output.as_deref().and_then(extract_json)).unwrap_or(Value::Null);

    let body = strip_json_block(strategist.output.as_deref().unwrap_or_default());
    let mut report = format!("# {}\n\n**Decision:** {}\n\n{}", ctx.task_title, ctx.decision.read().await, body.trim());
    if let Some(issues) = critic_report["issues"].as_array().filter(|issues| !issues.is_empty()) {
        report.push_str("\n\n## Red-team review\n");
        report.push_str(&format!("Verdict: **{}**\n", critic_report["verdict"].as_str().unwrap_or("n/a")));
        for issue in issues {
            report.push_str(&format!("- ({}) {}\n", issue["severity"].as_str().unwrap_or("note"), issue["problem"].as_str().unwrap_or_default()));
        }
    }
    let mut sources: Vec<(String, String)> = Vec::new();
    for agent in &agents {
        if let Some(items) = agent.sources.as_array() {
            for item in items {
                if let Some(url) = item["url"].as_str().filter(|url| url.starts_with("http")) {
                    if !sources.iter().any(|(existing, _)| existing == url) {
                        sources.push((url.to_string(), item["title"].as_str().unwrap_or(url).to_string()));
                    }
                }
            }
        }
    }
    if !sources.is_empty() {
        report.push_str("\n\n## Sources\n");
        for (url, title) in sources.iter().take(60) {
            report.push_str(&format!("- [{title}]({url})\n"));
        }
    }
    let trade_plan = strategist_report.get("trade_plan").cloned().filter(Value::is_object);
    if let Some(plan) = &trade_plan {
        report.push_str(&render_trade_plan(plan));
    }
    let summary = decision["recommendation"].as_str().map(String::from)
        .or_else(|| trade_plan.as_ref().map(|plan| format!("{} {} · {:.0}% · {}", plan["symbol"].as_str().unwrap_or(""), plan["direction"].as_str().unwrap_or("neutral").to_uppercase(), plan["probability"].as_f64().unwrap_or(0.5) * 100.0, plan["timing"].as_str().unwrap_or(""))))
        .or_else(|| strategist.summary.clone())
        .unwrap_or_else(|| "Decision ready.".into());

    let run_decision = json!({
        "recommendation": decision["recommendation"],
        "confidence": decision["confidence"],
        "options": decision["options"],
        "flip_conditions": decision["flip_conditions"],
        "next_actions": decision["next_actions"],
        "verdict": critic_report["verdict"],
        "sources": sources.len(),
        "trade_plan": trade_plan,
    });
    sqlx::query("UPDATE runs SET status = 'complete', report = $2, decision = $3, completed_at = NOW() WHERE id = $1").bind(ctx.run_id).bind(&report).bind(&run_decision).execute(database).await.map_err(|error| error.to_string())?;
    sqlx::query("UPDATE tasks SET status = 'complete', result = $2, completed_at = NOW() WHERE id = $1").bind(ctx.task_id).bind(&summary).execute(database).await.map_err(|error| error.to_string())?;

    if let (Some(plan), Some(trading)) = (&trade_plan, ctx.trading.read().await.clone()) {
        let harness = &ctx.state.harness;
        let symbol = match trading.symbol.clone() {
            Some(symbol) => Some(symbol),
            None => harness.quant.detect(plan["symbol"].as_str().unwrap_or_default()).await,
        };
        let recorded = match &symbol {
            Some(symbol) => super::market::record_prediction(database, &harness.quant, (ctx.project_id, ctx.task_id, ctx.run_id), symbol, plan, trading.horizon_hours, trading.model_prob_up, trading.as_of).await,
            None => None,
        };
        match recorded {
            Some(_) => ctx.event("prediction_recorded", format!("Prediction recorded for {}: {} at {:.0}% — scored automatically in {}h.", symbol.unwrap_or_default(), plan["direction"].as_str().unwrap_or("neutral"), plan["probability"].as_f64().unwrap_or(0.5) * 100.0, plan["horizon_hours"].as_i64().unwrap_or(trading.horizon_hours as i64))).from(ORCHESTRATOR).publish(&ctx.state).await,
            None => ctx.event("prediction_skipped", "Trade plan not recorded for scoring (no recognised instrument or no reference price from the quant sidecar).").from(ORCHESTRATOR).publish(&ctx.state).await,
        }
        // Replay: the horizon is already in the past, so the call is scored right away against what followed.
        if trading.as_of.is_some() {
            super::market::score_due(&ctx.state).await;
        }
    }
    {
        let scope = Scope { project_id: ctx.project_id, task_id: Some(ctx.task_id), run_id: Some(ctx.run_id), agent_key: Some(ORCHESTRATOR.into()) };
        memory::retain(&ctx.state.harness, &scope, vec![NewMemory { kind: "decision".into(), content: format!("Decision on \"{}\" ({}): {summary}", ctx.task_title, Utc::now().format("%Y-%m-%d")), entities: vec![], source_url: None, importance: 1.0, confidence: decision["confidence"].as_f64().map(|value| value as f32) }]).await;
        let consolidated = memory::consolidate(&ctx.state.harness, ctx.project_id, ctx.site(Some(orchestrator_id), true), &router::resolve(&ctx.settings, "utility")).await;
        if consolidated > 0 {
            ctx.event("memory_consolidated", format!("Consolidated findings into {consolidated} long-term observations.")).from(ORCHESTRATOR).publish(&ctx.state).await;
        }
    }
    // Outcome statistics for every skill used in this run feed the selection prior.
    let verdict = critic_report["verdict"].as_str().unwrap_or_default().to_ascii_lowercase();
    let (accepted, revised) = if verdict.contains("accept") { (1, 0) } else if verdict.contains("revise") || verdict.contains("another") { (0, 1) } else { (0, 0) };
    let used: HashSet<String> = agents.iter().flat_map(|agent| agent.skills.iter().cloned()).collect();
    for skill in &used {
        let _ = sqlx::query(
            "INSERT INTO skill_stats (skill, uses, accepted, revised) VALUES ($1, 1, $2, $3)
             ON CONFLICT (skill) DO UPDATE SET uses = skill_stats.uses + 1, accepted = skill_stats.accepted + $2, revised = skill_stats.revised + $3, updated_at = NOW()",
        )
        .bind(skill)
        .bind(accepted)
        .bind(revised)
        .execute(database)
        .await;
    }
    ctx.state.harness.refresh_skill_priors().await;
    set_status(ctx, orchestrator_id, "complete", None).await;
    let totals: (i64, i64, i64, i64, i32, i32) = sqlx::query_as("SELECT tokens_in, tokens_out, tokens_cache_read, tokens_saved, llm_calls, cache_hits FROM runs WHERE id = $1").bind(ctx.run_id).fetch_one(database).await.unwrap_or_default();
    ctx.event("run_complete", format!("Decision ready: {}", truncate_chars(&summary, 220)))
        .from(ORCHESTRATOR)
        .data(json!({"tokens_in": totals.0, "tokens_out": totals.1, "cache_read": totals.2, "tokens_saved": totals.3, "llm_calls": totals.4, "cache_hits": totals.5}))
        .publish(&ctx.state)
        .await;
    Ok(())
}

/// Resolves a task created in "auto" mode: RLCD (or the keyword rules when it is down or unsure) decides whether
/// the user wants a position or research and which trading style, the chart headers supply the instrument, and
/// the resolved configuration is stored on the task so the UI and later runs see the same thing.
async fn auto_route(state: &SharedState, ids: (Uuid, Uuid, Uuid), title: &str, brief: &str, mut config: Value) -> Value {
    if config["mode"].as_str() != Some("auto") {
        return config;
    }
    let (project_id, task_id, run_id) = ids;
    let harness = &state.harness;
    let attachments: Vec<std::path::PathBuf> = sqlx::query_scalar::<_, String>("SELECT path FROM attachments WHERE task_id = $1 ORDER BY created_at")
        .bind(task_id)
        .fetch_all(&state.database)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(std::path::PathBuf::from)
        .filter(|path| path.exists())
        .collect();
    let text = format!("{title}\n{brief}");
    let mut symbol = config["symbol"].as_str().map(str::trim).filter(|symbol| !symbol.is_empty()).map(String::from);
    if symbol.is_none() {
        symbol = harness.quant.detect(&text).await;
    }
    // Say something at once: routing can take a while when a chart has to be read, and a silent screen looks stuck.
    EventDraft::new(project_id, task_id, run_id, "intent_reading", "Working out what you are asking: a position or research, day trade or swing…").from(ORCHESTRATOR).publish(state).await;
    // The instrument is needed for the data pack. When the text does not name it, the chart header does: one
    // cheap vision call on the first two screenshots reads it. When the text already names it, skip the call.
    let mut chart_timeframes: Vec<String> = Vec::new();
    let mut charts = Value::Null;
    if symbol.is_none() && !attachments.is_empty() {
        EventDraft::new(project_id, task_id, run_id, "intent_reading", "Reading the instrument and timeframe from your chart header…").from(ORCHESTRATOR).publish(state).await;
        let settings = RunSettings::load(&state.database, &json!({})).await;
        let request = LlmRequest {
            provider: settings.provider,
            model: None,
            system: prompts::SYSTEM_CONSTITUTION.into(),
            prompt: prompts::chart_header_prompt(),
            web: false,
            timeout: Duration::from_secs(75),
            json_schema: Some(prompts::chart_header_schema()),
            progress: None,
            attachments: attachments.iter().take(2).cloned().collect(),
            effort: Some("low".into()),
        };
        if let Ok((response, _)) = harness.llm_routed(&request, &router::resolve(&settings, "utility"), CallSite { run_id, agent_id: None, cacheable: true }).await {
            if let Some(read) = response.structured.or_else(|| extract_json(&response.text)) {
                chart_timeframes = read["charts"].as_array().into_iter().flatten().filter_map(|chart| chart["timeframe"].as_str()).map(String::from).collect();
                if symbol.is_none() {
                    let named: Vec<&str> = read["charts"].as_array().into_iter().flatten().filter_map(|chart| chart["symbol"].as_str()).collect();
                    symbol = harness.quant.detect(&named.join(" ")).await;
                }
                charts = read;
            }
        }
    }
    let routed = harness.rlcd.route(&text, attachments.len(), symbol.as_deref(), &chart_timeframes).await;
    if let Some(object) = config.as_object_mut() {
        object.insert("mode".into(), json!(routed.mode));
        object.insert("routed_by".into(), json!(routed.source));
        object.insert("routing".into(), json!({"detail": routed.detail, "charts": charts}));
        if routed.mode == "trading" {
            object.insert("style".into(), json!(routed.style));
            if let Some(symbol) = &symbol {
                object.insert("symbol".into(), json!(symbol));
            }
            if routed.style == "daytrade" {
                // The day-trade risk rule is fixed: stop 20 pips, targets 50 and 100 pips.
                for (key, value) in [("sl_pips", 20.0), ("tp_pips", 50.0), ("tp2_pips", 100.0)] {
                    object.entry(key).or_insert(json!(value));
                }
                object.entry("depth").or_insert(json!("quick"));
                object.entry("horizon").or_insert(json!("8h"));
            } else {
                object.entry("horizon").or_insert(json!("1w"));
            }
        }
    }
    let _ = sqlx::query("UPDATE tasks SET config = $2 WHERE id = $1").bind(task_id).bind(&config).execute(&state.database).await;
    let summary = match (routed.mode, routed.style) {
        ("trading", "daytrade") => format!("Understood as a day-trade position request{} — technical desk, stop 20 pips, targets 50 and 100 pips.", symbol.as_ref().map(|symbol| format!(" on {symbol}")).unwrap_or_default()),
        ("trading", _) => format!("Understood as a swing position request{} — full desk with fundamentals and research.", symbol.as_ref().map(|symbol| format!(" on {symbol}")).unwrap_or_default()),
        _ => "Understood as a research request — research team.".to_string(),
    };
    EventDraft::new(project_id, task_id, run_id, "intent_detected", format!("{summary} (decided by {})", if routed.source == "rlcd" { "the RLCD model" } else { "keyword rules" }))
        .from(ORCHESTRATOR)
        .data(json!({"mode": routed.mode, "style": routed.style, "source": routed.source, "symbol": symbol, "detail": routed.detail, "charts": charts}))
        .publish(state)
        .await;
    config
}

/// Loads the trading desk context: instrument, data pack from the quant sidecar, chart screenshots.
/// Returns the desk section appended to the orchestrator's hiring prompt (empty for research tasks).
async fn prepare_trading(ctx: &RunContext, config: &Value) -> String {
    let harness = &ctx.state.harness;
    let attachments: Vec<std::path::PathBuf> = sqlx::query_scalar::<_, String>("SELECT path FROM attachments WHERE task_id = $1 ORDER BY created_at")
        .bind(ctx.task_id)
        .fetch_all(&ctx.state.database)
        .await
        .unwrap_or_default()
        .into_iter()
        .map(std::path::PathBuf::from)
        .filter(|path| path.exists())
        .collect();
    let explicit = config["symbol"].as_str().map(str::trim).filter(|symbol| !symbol.is_empty()).map(String::from);
    if config["mode"].as_str() != Some("trading") && explicit.is_none() {
        return String::new();
    }
    let symbol = match explicit {
        Some(symbol) => Some(symbol),
        None => harness.quant.detect(&format!("{} {}", ctx.task_title, ctx.task_brief)).await,
    };
    // Day-trade style: technical only (structure, sessions, retests) with fixed-pip risk; swing keeps the full quant desk.
    let daytrade = config["style"].as_str() == Some("daytrade");
    let as_of = config["as_of"].as_str().and_then(|text| DateTime::parse_from_rfc3339(text).ok()).map(|time| time.with_timezone(&Utc)).filter(|time| *time < Utc::now());
    let sl_pips = config["sl_pips"].as_f64().unwrap_or(20.0);
    let tp_pips = config["tp_pips"].as_f64().unwrap_or(50.0);
    let tp2_pips = config["tp2_pips"].as_f64().unwrap_or(100.0);
    let pip = config["pip"].as_f64().filter(|pip| *pip > 0.0);
    let horizon = config["horizon"].as_str().unwrap_or(if daytrade { "8h" } else { "1d" }).to_string();
    let horizon_hours = super::market::horizon_hours(&horizon);
    let timeframes: Vec<String> = config["timeframes"].as_array().map(|items| items.iter().filter_map(Value::as_str).map(String::from).collect::<Vec<_>>()).filter(|items| !items.is_empty()).unwrap_or_else(|| if daytrade { ["4h", "1h", "15m", "5m"].iter().map(|frame| frame.to_string()).collect() } else { default_timeframes(horizon_hours) });
    let mut market = String::new();
    let mut model_prob_up = None;
    match &symbol {
        Some(symbol) => {
            ctx.event("market_loading", format!("Loading market data pack for {symbol} ({}) · horizon {horizon}…", timeframes.join(", "))).from(ORCHESTRATOR).publish(&ctx.state).await;
            match harness.quant.snapshot(symbol, &timeframes, &horizon, as_of).await {
                Ok(snapshot) => {
                    market = snapshot["markdown"].as_str().unwrap_or_default().to_string();
                    model_prob_up = snapshot["prediction"]["prob_up"].as_f64();
                    let _ = sqlx::query("UPDATE runs SET market = $2 WHERE id = $1").bind(ctx.run_id).bind(&snapshot).execute(&ctx.state.database).await;
                    ctx.event("market_ready", format!("Market pack ready for {symbol}{}.", model_prob_up.map(|probability| format!(" · ML model P(up) {:.0}%", probability * 100.0)).unwrap_or_default()))
                        .from(ORCHESTRATOR)
                        .data(json!({"symbol": symbol, "prob_up": model_prob_up}))
                        .publish(&ctx.state)
                        .await;
                }
                Err(error) => ctx.event("market_unavailable", format!("Market data unavailable ({error}); the desk will rely on web research and charts.")).from(ORCHESTRATOR).publish(&ctx.state).await,
            }
        }
        None => ctx.event("market_unavailable", "Trading desk: no instrument recognised in the task text; the chart reader will identify it from the screenshot.").from(ORCHESTRATOR).publish(&ctx.state).await,
    }
    if let (true, Some(symbol)) = (daytrade, &symbol) {
        let mut sections: Vec<String> = Vec::new();
        let mut pip_size = pip;
        match harness.quant.session_story(symbol, "15m", pip, sl_pips, tp_pips, as_of).await {
            Ok(story) => {
                if let Some(markdown) = story["markdown"].as_str() { sections.push(format!("## Session story (how each session acted, where we are now, next triggers)\n{markdown}")) }
                ctx.event("session_story", format!("Session story loaded for {symbol}: last sessions, current lines and next triggers.")).from(ORCHESTRATOR).data(json!({"now": story["now"], "next_actions": story["next_actions"]})).publish(&ctx.state).await;
            }
            Err(error) => ctx.event("market_unavailable", format!("Session story unavailable ({error}).")).from(ORCHESTRATOR).publish(&ctx.state).await,
        }
        let mut mtf_query = vec![("symbol".to_string(), symbol.clone()), ("intervals".to_string(), timeframes.join(",")), ("sl_pips".to_string(), sl_pips.to_string()), ("tp_pips".to_string(), tp_pips.to_string()), ("tp2_pips".to_string(), tp2_pips.to_string())];
        if let Some(pip) = pip { mtf_query.push(("pip".to_string(), pip.to_string())) }
        if let Some(as_of) = as_of { mtf_query.push(("as_of".to_string(), as_of.to_rfc3339())) }
        match harness.quant.mtf(&mtf_query).await {
            Ok(mtf) => {
                pip_size = pip_size.or(mtf["params"]["pip"].as_f64().filter(|pip| *pip > 0.0));
                if let Some(markdown) = mtf["markdown"].as_str() { sections.push(format!("## Top-down read (H4 context → H1 setup → M15/M5 trigger: boxes, impulses, failed sweeps, playbook, measured odds)\n{markdown}")) }
                ctx.event("top_down", format!("Top-down read loaded for {symbol}: {}.", mtf["playbook"]["mode"].as_str().unwrap_or("no playbook"))).from(ORCHESTRATOR).data(json!({"playbook": mtf["playbook"], "stack": mtf["stack"], "needs": mtf["needs"]})).publish(&ctx.state).await;
            }
            Err(error) => ctx.event("market_unavailable", format!("Top-down read unavailable ({error}); the head trader needs H4 and H1 screenshots to judge the range.")).from(ORCHESTRATOR).publish(&ctx.state).await,
        }
        let mut rlcd_request = json!({"symbol": symbol, "interval": "15m", "sl_pips": sl_pips, "tp_pips": tp_pips, "tp2_pips": tp2_pips, "text": ctx.task_title});
        if let Some(pip) = pip { rlcd_request["pip"] = json!(pip) }
        if let Some(as_of) = as_of { rlcd_request["as_of"] = json!(as_of.to_rfc3339()) }
        match harness.rlcd.decide(&rlcd_request).await {
            Ok(decision) => {
                sections.push(format!("## RLCD calibrated decision (outcome-trained model; M15 entry, fixed stop and targets)\n{}", super::rlcd::decision_markdown(&decision)));
                let _ = sqlx::query("UPDATE runs SET market = jsonb_set(COALESCE(market, '{}'::jsonb), '{rlcd}', $2) WHERE id = $1").bind(ctx.run_id).bind(&decision).execute(&ctx.state.database).await;
                ctx.event("rlcd_decision", format!("RLCD: {} ({}) · buy {:.0}% · sell {:.0}% · neither {:.0}%.", decision["action"].as_str().unwrap_or("hold").to_uppercase(), decision["tier"].as_str().unwrap_or("hold"), decision["probabilities"]["buy"].as_f64().unwrap_or(0.0) * 100.0, decision["probabilities"]["sell"].as_f64().unwrap_or(0.0) * 100.0, decision["probabilities"]["hold"].as_f64().unwrap_or(0.0) * 100.0)).from(ORCHESTRATOR).data(decision).publish(&ctx.state).await;
            }
            Err(error) => tracing::debug!("rlcd decision skipped: {error}"),
        }
        let mut levels_query = vec![("symbol".to_string(), symbol.clone()), ("intervals".to_string(), "4h,1h,15m".to_string())];
        if let Some(pip) = pip { levels_query.push(("pip".to_string(), pip.to_string())) }
        if let Some(as_of) = as_of { levels_query.push(("as_of".to_string(), as_of.to_rfc3339())) }
        if let Ok(levels) = harness.quant.levels(&levels_query).await {
            if let Some(markdown) = levels["markdown"].as_str() { sections.push(format!("## Lines and reactions (zones, touches, hold-vs-break odds, M15 → H1 → H4 stack)\n{markdown}")) }
        }
        let mut retest_query = vec![("symbol".to_string(), symbol.clone()), ("interval".to_string(), "15m".to_string()), ("level_interval".to_string(), "1h".to_string()), ("sl_pips".to_string(), sl_pips.to_string()), ("tp_pips".to_string(), tp_pips.to_string())];
        if let Some(pip) = pip { retest_query.push(("pip".to_string(), pip.to_string())) }
        if let Some(as_of) = as_of { retest_query.push(("as_of".to_string(), as_of.to_rfc3339())) }
        if let Ok(retest) = harness.quant.retest(&retest_query).await {
            if let Some(markdown) = retest["markdown"].as_str() { sections.push(format!("## Retest lab (history of this exact entry model)\n{markdown}")) }
        }
        let rules = format!(
            "## Desk rules (day trade — binding)\nTechnical only: body-close structure lines, sessions, break → retest → rejection entries. News is timing only (blackout around releases), never a reason to enter.\nRead top-down: H4 = context, H1 = setup, M15/M5 = trigger. A lower timeframe trending inside a higher-timeframe consolidation is noise until an edge of that box is reached.\nWhen H4/H1 consolidate and price is inside the box: the plan is a RANGE plan — a sell zone at the top edge and a buy zone at the bottom edge, each with its own stop, targets and M15/M5 trigger; the middle of the box is no trade.\nA wick beyond an edge that closes back inside is a sweep, not a break. Only a body close beyond the edge on the setup timeframe breaks the range; then the plan is break → retest of that edge.\nPrices: the user's screenshots show their broker's prices and candle closes and are authoritative for exact levels. The data pack comes from a free feed that can sit several dollars/pips away from the broker and can close a candle on the other side of a line. When both exist, quote every level in screenshot prices, state the offset you see between the two, and use the data pack for structure, history and odds only. Without screenshots, say that levels are from the free feed and must be matched to the user's chart before acting.\nRisk: stop {sl_pips} pips beyond the line; targets {tp_pips} and {tp2_pips} pips{}. Entries on M15/M5 only, in the direction the H4/H1 read allows.\n",
            pip_size.map(|pip| format!(". PIP SIZE: 1 pip = {pip} in price, so the stop is {:.2}, the targets are {:.2} and {:.2} in price, and a 1.00 move is {:.0} pips. Convert every pip figure with this size and never treat a pip as one whole price unit; a stop or target that does not match these distances is wrong unless you state why you deviate", sl_pips * pip, tp_pips * pip, tp2_pips * pip, 1.0 / pip)).unwrap_or_default()
        );
        let now = as_of.unwrap_or_else(Utc::now);
        let rules = format!("{rules}Now: {} UTC. Use this weekday and date for every session and release time you name.\n", now.format("%A %Y-%m-%d %H:%M"));
        sections.insert(0, rules);
        sections.push(market);
        market = sections.join("\n\n");
    }
    // Headlines from free sources, as timing context and reasons to wait (never in replay: that would leak the future).
    if let (Some(symbol), None) = (&symbol, as_of) {
        let mut hits = super::web::search(&ctx.state.database, &harness.http, &format!("{} price news today", headline_subject(symbol)), 6).await;
        // Policy posts and statements move gold and the dollar within minutes; collect what a free search can see.
        for hit in super::web::search(&ctx.state.database, &harness.http, "Trump Truth Social post today tariffs Fed dollar markets", 4).await {
            if !hits.iter().any(|existing| existing.url == hit.url) {
                hits.push(hit);
            }
        }
        if !hits.is_empty() {
            let lines: Vec<String> = hits.iter().map(|hit| format!("- {} — {} ({})", truncate_chars(&hit.title, 140), truncate_chars(&hit.snippet, 220), hit.url)).collect();
            market = format!("{market}\n\n## Headlines (free web search, unverified; use for timing and risk only)\n{}", lines.join("\n"));
        }
    }
    if !attachments.is_empty() {
        ctx.event("charts_attached", format!("{} chart screenshot(s) attached for the chart reader.", attachments.len())).from(ORCHESTRATOR).publish(&ctx.state).await;
    }
    if let Some(as_of) = as_of {
        market = format!("## REPLAY MODE\nThe current moment is {} UTC. You know nothing that happened after it: no web browsing, no later prices or news. Analyse exactly as if this were live.\n\n{market}", as_of.format("%Y-%m-%d %H:%M"));
        ctx.event("replay_mode", format!("Replay: analysing as of {} UTC with no look-ahead; the plan is scored immediately against what followed.", as_of.format("%Y-%m-%d %H:%M"))).from(ORCHESTRATOR).publish(&ctx.state).await;
    }
    let brief = if daytrade { prompts::daytrade_brief(symbol.as_deref(), horizon_hours, &timeframes, attachments.len(), &market, sl_pips, tp_pips, tp2_pips) } else { prompts::desk_brief(symbol.as_deref(), horizon_hours, &timeframes, attachments.len(), &market) };
    *ctx.trading.write().await = Some(Trading { symbol, horizon_hours, market, model_prob_up, attachments, as_of });
    brief
}

/// Search subject for headlines: the name news sites use for the instrument.
fn headline_subject(symbol: &str) -> String {
    let upper = symbol.to_uppercase();
    match upper.as_str() {
        "XAUUSD" => "gold XAUUSD".into(),
        "XAGUSD" => "silver XAGUSD".into(),
        _ if upper.len() == 6 && upper.chars().all(|c| c.is_ascii_alphabetic()) => format!("{}/{} forex", &upper[..3], &upper[3..]),
        _ => upper,
    }
}

fn default_timeframes(horizon_hours: i32) -> Vec<String> {
    let frames: &[&str] = match horizon_hours {
        0..=6 => &["4h", "1h", "15m"],
        7..=48 => &["1d", "4h", "1h"],
        49..=240 => &["1wk", "1d", "4h"],
        _ => &["1mo", "1wk", "1d"],
    };
    frames.iter().map(|frame| frame.to_string()).collect()
}

fn render_trade_plan(plan: &Value) -> String {
    let number = |value: &Value| value.as_f64().map(|value| format!("{value}")).unwrap_or_else(|| "–".into());
    let list = |value: &Value| value.as_array().map(|items| items.iter().map(|item| item.as_f64().map(|v| v.to_string()).or_else(|| item.as_str().map(String::from)).unwrap_or_default()).collect::<Vec<_>>().join(", ")).unwrap_or_default();
    let mut out = format!(
        "\n\n## Trade plan\n| | |\n|---|---|\n| Instrument | {} |\n| Direction | **{}** |\n| Probability | {:.0}% |\n| Horizon | {}h |\n| Entry zone | {} |\n| Stop | {} |\n| Targets | {} |\n| Timing | {} |\n| Invalidation | {} |\n",
        plan["symbol"].as_str().unwrap_or("–"),
        plan["direction"].as_str().unwrap_or("neutral").to_uppercase(),
        plan["probability"].as_f64().unwrap_or(0.5) * 100.0,
        plan["horizon_hours"].as_i64().unwrap_or(0),
        list(&plan["entry_zone"]),
        number(&plan["stop"]),
        list(&plan["targets"]),
        plan["timing"].as_str().unwrap_or("–"),
        plan["invalidation"].as_str().unwrap_or("–"),
    );
    if let Some(frames) = plan["timeframes"].as_array().filter(|frames| !frames.is_empty()) {
        out.push_str("\n**Top-down read**\n");
        for frame in frames {
            out.push_str(&format!("- **{}** — {}: {}\n", frame["tf"].as_str().unwrap_or("?"), frame["state"].as_str().unwrap_or("?").replace('_', " "), frame["read"].as_str().unwrap_or_default()));
        }
    }
    if let Some(zones) = plan["zones"].as_array().filter(|zones| !zones.is_empty()) {
        out.push_str("\n**Zones**\n| | Zone | Stop | Targets | Trigger |\n|---|---|---|---|---|\n");
        for zone in zones {
            out.push_str(&format!("| {} | {} | {} | {} | {} |\n", zone["side"].as_str().unwrap_or("?").to_uppercase(), list(&zone["zone"]), number(&zone["stop"]), list(&zone["targets"]), zone["trigger"].as_str().unwrap_or("–").replace('|', "/")));
        }
    }
    if let Some(reasons) = plan["reasons"].as_array().filter(|reasons| !reasons.is_empty()) {
        out.push_str("\n**Why**\n");
        for reason in reasons.iter().filter_map(Value::as_str) {
            out.push_str(&format!("- {reason}\n"));
        }
    }
    if let Some(cases) = plan["cases"].as_array().filter(|cases| !cases.is_empty()) {
        out.push_str("\n**Decision table**\n| Action | When | Entry | Stop | Targets | Reason | Risk |\n|---|---|---|---|---|---|---|\n");
        let cell = |value: &Value| value.as_str().unwrap_or("–").replace('|', "/").replace('\n', " ");
        for case in cases {
            out.push_str(&format!("| {} | {} | {} | {} | {} | {} | {} |\n", case["action"].as_str().unwrap_or("?").to_uppercase(), cell(&case["when"]), number(&case["entry"]), number(&case["stop"]), list(&case["targets"]), cell(&case["reason"]), cell(&case["risk"])));
        }
    }
    if let Some(check) = plan["thesis_check"].as_object().filter(|check| check.get("view").and_then(Value::as_str).is_some_and(|view| !view.is_empty())) {
        out.push_str(&format!("\n**Your prediction** ({}): {}\n- Confirms: {}\n- Invalidates: {}\n", check.get("verdict").and_then(Value::as_str).unwrap_or("?").replace('_', " "), check["view"].as_str().unwrap_or_default(), check.get("confirms").and_then(Value::as_str).unwrap_or("–"), check.get("invalidates").and_then(Value::as_str).unwrap_or("–")));
    }
    if let Some(news) = plan["news"].as_array().filter(|news| !news.is_empty()) {
        out.push_str("\n**News**\n");
        for item in news {
            out.push_str(&format!("- {} — {}: {}\n", item["time"].as_str().unwrap_or("time n/a"), item["event"].as_str().unwrap_or("event"), item["effect"].as_str().unwrap_or_default()));
        }
    }
    if let Some(risks) = plan["risks"].as_array().filter(|risks| !risks.is_empty()) {
        out.push_str("\n**Risks**\n");
        for risk in risks.iter().filter_map(Value::as_str) {
            out.push_str(&format!("- {risk}\n"));
        }
    }
    for (label, key) in [("To continue up", "up"), ("To continue down", "down")] {
        if let Some(text) = plan["continuation"][key].as_str().filter(|text| !text.is_empty()) {
            out.push_str(&format!("\n**{label}:** {text}\n"));
        }
    }
    if let Some(needs) = plan["needs"].as_array().filter(|needs| !needs.is_empty()) {
        out.push_str("\n**Needed for a firmer read:** ");
        out.push_str(&needs.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(" · "));
        out.push('\n');
    }
    if let Some(events) = plan["key_events"].as_array().filter(|events| !events.is_empty()) {
        out.push_str("\n**Key events:** ");
        out.push_str(&events.iter().filter_map(Value::as_str).collect::<Vec<_>>().join(" · "));
        out.push('\n');
    }
    if let Some(scenarios) = plan["scenarios"].as_array().filter(|scenarios| !scenarios.is_empty()) {
        out.push_str("\n**Scenarios**\n");
        for scenario in scenarios {
            out.push_str(&format!("- {} ({:.0}%): {}\n", scenario["name"].as_str().unwrap_or("scenario"), scenario["probability"].as_f64().unwrap_or(0.0) * 100.0, scenario["path"].as_str().unwrap_or_default()));
        }
    }
    out
}

fn strip_json_block(text: &str) -> String {
    match text.rfind("```json") {
        Some(index) => text[..index].to_string(),
        None => text.to_string(),
    }
}
