export type TaskStatus = 'ready' | 'running' | 'complete' | 'failed'
export type AgentStatus = 'pending' | 'running' | 'retrying' | 'complete' | 'failed' | 'skipped'
export type AgentKind = 'orchestrator' | 'intent' | 'researcher' | 'analyst' | 'strategist' | 'critic' | 'specialist'

export type TaskConfig = Partial<{ provider: string; model: string; depth: 'quick' | 'standard' | 'deep'; max_agents: number; mode: 'auto' | 'research' | 'trading'; routed_by: 'rlcd' | 'keywords'; routing: { charts?: { charts?: { symbol?: string | null; timeframe?: string | null }[] } | null }; symbol: string; horizon: string; timeframes: string[]; style: 'daytrade' | 'swing'; sl_pips: number; tp_pips: number; tp2_pips: number; pip: number; as_of: string }>

export type Task = {
  id: string; project_id: string; title: string; description: string; status: TaskStatus; config: TaskConfig
  result: string | null; latest_run_id: string | null; run_started_at: string | null; completed_at: string | null; created_at: string
}
export type Project = { id: string; name: string; description: string; created_at: string; tasks: Task[]; memory_count: number }

export type SwarmEvent = {
  id: string; project_id: string; task_id: string | null; run_id: string | null; kind: string; message: string
  from_agent_id: string | null; to_agent_id: string | null; data: Record<string, unknown> | null; created_at: string
}

/** RLCD's calibrated day-trade call: which action reaches its target before its stop, with the fixed pip risk. */
export type RlcdDecision = {
  decision_id?: string; model?: string; symbol?: string; interval?: string; time?: string; price?: number; pip?: number
  action?: 'buy' | 'sell' | 'hold'; tier?: 'act' | 'confirm' | 'hold'; probabilities?: { buy?: number; sell?: number; hold?: number }; confidence?: number
  breakeven_probability?: number; expected_r?: { buy?: number; sell?: number }; entry?: number; stop?: number; targets?: number[]
  reasons?: unknown[]; warnings?: string[]
}
/** The annotated chart from the sidecar: candles plus every drawing and the plain reading. */
export type ChartData = {
  symbol: string; interval: string; pip?: number; as_of?: string; price: number; data_note?: string
  candles: { t: string; o: number; h: number; l: number; c: number; forming?: boolean }[]
  swings?: { t: string; price: number; kind: 'high' | 'low'; label: string }[]
  boxes?: { interval: string; start: string; end: string | null; top: number; bottom: number; state: string }[]
  zones?: { level: number; low?: number; high?: number; role: 'support' | 'resistance'; timeframes?: string[]; touches?: number }[]
  events?: { t: string; price: number; type: string; text?: string }[]
  impulses?: { start: string; end: string; from: number; to: number; direction: 'up' | 'down' }[]
  patterns?: { name: string; kind?: string; direction?: string | null; state?: string; points?: { t: string; price: number }[]; text?: string }[]
  sessions?: { name: string; start: string; end: string }[]
  news?: { t: string; currency?: string; type?: string; title?: string; past?: boolean }[]
  playbook?: { mode?: string; cases?: { action: 'sell' | 'buy' | 'hold'; when?: string; entry_price?: number | null; stop?: number | null; targets?: number[]; reason?: string; risk?: string }[] }
  higher?: Record<string, { regime?: string; box?: { top: number; bottom: number } | null }>
  reading?: { regime?: string; phase?: string; summary?: string; what_next?: string[]; wait_for?: string[]; risk?: { level?: 'low' | 'medium' | 'high'; reasons?: string[]; note?: string } }
}
export type DecisionOption = { name: string; score?: number; pros?: string[]; cons?: string[] }
export type TradePlan = {
  symbol?: string; direction?: 'long' | 'short' | 'neutral'; probability?: number; horizon_hours?: number; entry_zone?: number[]; stop?: number; targets?: number[]
  timing?: string; key_events?: string[]; scenarios?: { name: string; probability?: number; path?: string }[]; invalidation?: string
  mode?: 'range' | 'break_retest' | 'trend' | 'wait'
  timeframes?: { tf: string; state?: string; read?: string; source?: 'screenshot' | 'data' }[]
  zones?: { side: 'sell' | 'buy'; zone?: number[]; stop?: number; targets?: number[]; trigger?: string; why?: string }[]
  reasons?: string[]; continuation?: { up?: string; down?: string }; needs?: string[]
  cases?: { action: 'sell' | 'buy' | 'hold'; when?: string; entry?: number; stop?: number; targets?: number[]; reason?: string; risk?: string }[]
  news?: { time?: string; event?: string; effect?: string; source?: string }[]; risks?: string[]
  thesis_check?: { view?: string; verdict?: 'supported' | 'partly' | 'not_supported'; confirms?: string; invalidates?: string }
}
export type Decision = {
  recommendation?: string; confidence?: number; options?: DecisionOption[]; flip_conditions?: string[]
  next_actions?: { action: string; owner?: string; when?: string }[]; verdict?: string; sources?: number; trade_plan?: TradePlan | null
}

export type Run = {
  id: string; project_id: string; task_id: string; status: 'planning' | 'running' | 'complete' | 'failed' | 'interrupted'; round: number
  provider: string; model: string | null; config: Record<string, unknown>
  intent: { decision_to_make?: string; intent?: { goal?: string; success_criteria?: string[]; assumptions?: string[]; unknowns?: string[] }; rationale?: string } | null
  report: string | null; decision: Decision | null; error: string | null
  market: { markdown?: string; prediction?: { prob_up?: number; validation?: { accuracy?: number; baseline_accuracy?: number; brier?: number } }; rlcd?: RlcdDecision } | null
  tokens_in: number; tokens_out: number; tokens_cache_read: number; tokens_cache_write: number; tokens_saved: number
  llm_calls: number; cache_hits: number; started_at: string; completed_at: string | null
}

export type RunAgent = {
  id: string; run_id: string; key: string; name: string; role: string; kind: AgentKind; objective: string
  reports_to: string | null; hired_by: string | null; skills: string[]; depends_on: string[]; round: number
  status: AgentStatus; attempt: number; provider: string; model: string | null
  max_iterations: number; iterations: number; output: string | null; summary: string | null
  sources: { title?: string; url: string }[]; tokens_in: number; tokens_out: number; tokens_cache_read: number
  tokens_cache_write: number; error: string | null; started_at: string | null; completed_at: string | null; created_at: string
}

export type RunDetail = { run: Run; agents: RunAgent[]; events: SwarmEvent[]; active: boolean }
export type Dashboard = { projects: Project[]; events: SwarmEvent[]; skills: number }

export type Skill = { name: string; description: string; domain: string; tags: string[]; tokens: number; body?: string; source?: string; stats?: { uses: number; accepted: number; revised: number; hits: number; misses: number } | null }
export type SkillImport = { source: string; found: number; imported: string[]; skipped_existing: string[]; failed: { path: string; reason: string }[] }
export type Provider = { id: string; available: boolean; detail: string; model: string | null; native_web: boolean; tested: boolean }
export type Settings = {
  provider: string; model: string | null; utility_model: string | null; routes: Record<string, string>; fallbacks: string[]
  depth: string; max_agents: number; concurrency: number; max_iterations: number; max_rounds: number; max_attempts: number; agent_timeout_secs: number; allow_agent_hiring: boolean; effort?: string | null
}
export type MemoryItem = { id: string; kind: string; content: string; source_url: string | null; proof_count: number; importance: number; created_at: string; score?: number }

export function timeLabel(iso: string) { return new Date(iso).toLocaleTimeString([], { hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }) }
export function formatTokens(value: number) { return value >= 1_000_000 ? `${(value / 1_000_000).toFixed(1)}M` : value >= 1000 ? `${(value / 1000).toFixed(1)}k` : String(value) }
export const ROUTE_ROLES = ['orchestrator', 'intent', 'researcher', 'analyst', 'specialist', 'strategist', 'critic', 'utility'] as const
export function agentReport(agent: RunAgent): Record<string, unknown> | null {
  const text = agent.output ?? ''
  const fence = text.lastIndexOf('```json')
  if (fence === -1) return null
  const body = text.slice(fence + 7, text.indexOf('```', fence + 7) === -1 ? undefined : text.indexOf('```', fence + 7))
  try { return JSON.parse(body) } catch { return null }
}
export function stripJsonBlock(text: string) { const fence = text.lastIndexOf('```json'); return fence === -1 ? text : text.slice(0, fence) }
export type Attachment = { id: string; filename: string; mime: string; bytes: number; created_at: string }
export type Prediction = {
  id: string; task_id: string; symbol: string; direction: string; probability: number; horizon_hours: number; reference_price: number
  stop: number | null; targets: number[]; outcome: string | null; return_pct: number | null; brier: number | null; created_at: string; evaluate_at: string
}
export type PredictionBook = { summary: { total: number; scored: number; hits: number; hit_rate: number | null; direction_rate: number | null; brier: number | null }; items: Prediction[] }
export type MarketSymbol = { id: string; name?: string; asset_class?: string }
export type WatchItem = { id: string; symbol: string; intervals: string[]; active: boolean; last_checked_at?: string | null; last_error?: string | null }
export type BacktestStats = { n_trades?: number; win_rate?: number | null; win_rate_ci95?: number[] | null; breakeven_win_rate?: number; expectancy_r?: number | null; profit_factor?: number | null; total_r?: number; max_drawdown_r?: number; longest_losing_streak?: number; verdict?: string }
export type Backtest = {
  symbol: string; interval: string; period_start?: string; period_end?: string; overall: BacktestStats
  by_session?: Record<string, BacktestStats>; by_type?: Record<string, BacktestStats>; by_htf_alignment?: Record<string, BacktestStats>
  equity_curve?: { n: number; cum_r: number }[]; notes?: string[] | string
}
