import { useEffect, useState } from 'react'
import { api } from '../api'
import type { RlcdDecision } from '../types'
import { RlcdBlock } from './Trading'

type Metrics = { brier_skill_score?: number; ece?: number; directional_auc?: number; accuracy?: number; has_skill?: boolean; n_holdout?: number; holdout_from?: string; holdout_to?: string; policy?: { verdict?: string } }
type Head = { name: string; type: string; kind?: string; trained?: boolean; n_train?: number; model_version?: string; trained_at?: string; description?: string; metrics?: Metrics }
type Segment = { n_holdout?: number; brier_skill_score?: number; directional_auc?: number; has_skill?: boolean }
type Calibration = { brier_skill_score?: number; ece?: number; log_loss?: number; directional_auc?: number; n_train?: number; n_holdout?: number; feature_version?: string; by_segment?: Record<string, Segment>; by_year?: Record<string, Segment>; baseline?: { log_loss?: number; ece?: number } }
type Bin = { action: string; lo: number; hi: number; wins: number; n: number; mean: number; ci95: [number, number]; p_above_breakeven: number }
type Trades = { n?: number; win_rate?: number | null; mean_r?: number | null; breakeven_rate?: number | null; wilson_95?: [number | null, number | null] }
type Test = { n?: number; p_value?: number | null; win_rate?: number; breakeven?: number; trades_needed_80_power?: number | null }
type Bernoulli = { bins?: Bin[]; breakeven?: number; payoff_b?: number; holdout_trades?: { act?: Trades; act_or_confirm?: Trades }; binomial_test?: { act?: Test; act_or_confirm?: Test }; config?: { min_p_above_breakeven?: number } }
type Job = { status?: string; error?: string | null; progress?: { done?: number; total?: number; message?: string } }

const pct = (value: number | null | undefined, digits = 1) => typeof value === 'number' ? `${(value * 100).toFixed(digits)}%` : '–'
const num = (value: number | null | undefined, digits = 3) => typeof value === 'number' ? value.toFixed(digits) : '–'
const errorText = (caught: unknown) => caught instanceof Error ? caught.message : String(caught)

/** RLCD: what the calibrated decision model knows, measured on history it never trained on. */
export function RlcdPanel() {
  const [health, setHealth] = useState<Record<string, unknown> | null>(null)
  const [heads, setHeads] = useState<Head[]>([])
  const [selected, setSelected] = useState('setup:15m')
  const [calibration, setCalibration] = useState<Calibration | null>(null)
  const [bernoulli, setBernoulli] = useState<Bernoulli | null>(null)
  const [error, setError] = useState('')
  const [job, setJob] = useState<{ id: string; state: Job } | null>(null)
  const [scan, setScan] = useState<{ loading: boolean; rows?: RlcdDecision[]; error?: string } | null>(null)

  const load = () => {
    void api.rlcdHealth().then(setHealth).catch((caught) => setError(errorText(caught)))
    void api.rlcdHeads().then((value) => setHeads(((value as { heads?: Head[] }).heads ?? []))).catch(() => setHeads([]))
  }
  useEffect(load, [])
  useEffect(() => {
    setCalibration(null); setBernoulli(null)
    void api.rlcdCalibration(selected).then((value) => setCalibration(value as Calibration)).catch(() => setCalibration(null))
    if (selected.startsWith('setup:')) void api.rlcdBernoulli(selected).then((value) => setBernoulli(value as Bernoulli)).catch(() => setBernoulli(null))
  }, [selected, heads.length])
  // Training runs for minutes: poll the job until it ends, then reload the reports.
  useEffect(() => {
    if (!job || job.state.status !== 'running') return
    const timer = window.setInterval(() => void api.rlcdTrainStatus(job.id).then((state) => { setJob({ id: job.id, state: state as Job }); if ((state as Job).status !== 'running') load() }).catch(() => undefined), 4000)
    return () => window.clearInterval(timer)
  }, [job])

  async function train() {
    setError('')
    try { const started = await api.rlcdTrain({}) as { job_id?: string }; if (started.job_id) setJob({ id: started.job_id, state: { status: 'running' } }) } catch (caught) { setError(errorText(caught)) }
  }
  async function runScan() {
    setScan({ loading: true })
    try { const result = await api.rlcdScan() as { results?: (RlcdDecision & { decision?: RlcdDecision })[] }; setScan({ loading: false, rows: (result.results ?? []).map((row) => ({ ...(row.decision ?? row), symbol: row.symbol })) }) } catch (caught) { setScan({ loading: false, error: errorText(caught) }) }
  }

  const down = health?.status !== 'ok'
  const setups = heads.filter((head) => head.type === 'choice' || head.kind === 'pattern')
  const trades = bernoulli?.holdout_trades?.act_or_confirm
  const test = bernoulli?.binomial_test?.act_or_confirm
  return <div className="rlcd-panel">
    <p className="orchestrator-copy">RLCD answers typed questions with probabilities instead of text: what a request is asking for, and which of buy, sell or neither reaches its target before its stop (stop 20 pips, targets 50 and 100 pips). Its numbers below come from the most recent 20% of history, which it never trained on.</p>
    {error && <p className="run-error">{error}</p>}
    <div className="rlcd-status">
      <span><i className={`roster-dot ${down ? 'failed' : 'running'}`} />{down ? 'not running — start it with make rlcd' : `model ${String(health?.model ?? '')}`}</span>
      <button type="button" disabled={down || job?.state.status === 'running'} onClick={() => void train()}>{job?.state.status === 'running' ? 'TRAINING…' : 'RETRAIN'}</button>
      <button type="button" disabled={down || scan?.loading} onClick={() => void runScan()}>{scan?.loading ? 'SCANNING…' : 'SCAN MAJORS NOW'}</button>
    </div>
    {job && <p className="empty">{job.state.status === 'running' ? `Training ${job.state.progress?.done ?? 0}/${job.state.progress?.total ?? '?'} — ${job.state.progress?.message ?? 'starting'}` : job.state.error ? `Training failed: ${job.state.error}` : 'Training finished; reports reloaded.'}</p>}
    {scan?.error && <p className="run-error">{scan.error}</p>}
    {scan?.rows && <div className="rlcd-scan">{scan.rows.length === 0 ? <p className="empty">No instruments returned.</p> : scan.rows.map((row) => <div key={row.symbol}><b>{row.symbol}</b><RlcdBlock decision={row} /></div>)}</div>}

    <table className="options"><thead><tr><th>Head</th><th>answers</th><th>trained on</th><th>skill vs base rate</th><th>calibration error</th><th>direction (0.5 = none)</th></tr></thead><tbody>
      {setups.map((head) => <tr key={head.name} className={head.name === selected ? 'active' : ''} onClick={() => setSelected(head.name)}>
        <td>{head.name}</td><td>{head.type}</td><td>{head.trained ? (head.n_train ?? 0).toLocaleString() : 'untrained'}</td>
        <td>{pct(head.metrics?.brier_skill_score)}</td><td>{pct(head.metrics?.ece)}</td><td>{num(head.metrics?.directional_auc)}</td>
      </tr>)}
    </tbody></table>

    {calibration && <div className="rlcd-report">
      <div className="field-label">{selected.toUpperCase()} <small>({(calibration.n_train ?? 0).toLocaleString()} training rows · {(calibration.n_holdout ?? 0).toLocaleString()} held back · inputs {calibration.feature_version ?? '–'})</small></div>
      {trades && <p className={`rlcd-verdict ${typeof test?.p_value === 'number' && test.p_value <= 0.1 && (trades.mean_r ?? 0) > 0 ? 'edge' : 'none'}`}>
        Trades it would have taken on held-back history: <b>{trades.n ?? 0}</b> · win rate <b>{pct(trades.win_rate)}</b> ({pct(trades.wilson_95?.[0])} – {pct(trades.wilson_95?.[1])}) against breakeven <b>{pct(trades.breakeven_rate)}</b> · mean {num(trades.mean_r, 2)}R · p-value against breakeven <b>{num(test?.p_value, 2)}</b> <small>(below 0.10 would count as an edge)</small>
        {test?.trades_needed_80_power ? ` · about ${test.trades_needed_80_power} trades needed to confirm` : ''}
      </p>}
      {bernoulli?.bins?.length ? <table className="options"><thead><tr><th>When it says</th><th>probability band</th><th>won</th><th>of</th><th>real rate</th><th>95% range</th><th>beats breakeven</th></tr></thead><tbody>
        {bernoulli.bins.filter((bin) => bin.n > 0).map((bin) => <tr key={`${bin.action}${bin.lo}`}><td>{bin.action}</td><td>{pct(bin.lo, 0)} – {pct(bin.hi, 0)}</td><td>{bin.wins}</td><td>{bin.n}</td><td>{pct(bin.mean)}</td><td>{pct(bin.ci95[0])} – {pct(bin.ci95[1])}</td><td>{pct(bin.p_above_breakeven, 0)}</td></tr>)}
      </tbody></table> : null}
      {calibration.by_segment && <table className="options"><thead><tr><th>Instrument</th><th>held back</th><th>skill vs base rate</th><th>direction</th></tr></thead><tbody>
        {Object.entries(calibration.by_segment).map(([segment, value]) => <tr key={segment}><td>{segment.split('@')[0]}</td><td>{(value.n_holdout ?? 0).toLocaleString()}</td><td>{pct(value.brier_skill_score)}</td><td>{num(value.directional_auc)}</td></tr>)}
      </tbody></table>}
    </div>}
  </div>
}
