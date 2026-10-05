import { FormEvent, Suspense, lazy, useEffect, useState } from 'react'
import { Plus, X } from 'lucide-react'
import { api, attachmentUrl } from '../api'
import { timeLabel, type RlcdDecision, type Attachment, type Prediction, type Backtest, type BacktestStats, type PredictionBook, type Run, type TradePlan, type WatchItem } from '../types'
import { Markdown } from './Markdown'

const pct = (value: number | null | undefined) => typeof value === 'number' ? `${Math.round(value * 100)}%` : '–'

const ChartView = lazy(() => import('./ChartView'))

/** Opens View Engine's own annotated chart of an instrument: zones, HH/HL/LH/LL, boxes, sweeps, the reading. */
export function ChartButton({ symbol, interval, asOf, label = 'DRAW IT ON THE CHART' }: { symbol: string; interval?: string; asOf?: string; label?: string }) {
  const [open, setOpen] = useState(false)
  return <>
    <button type="button" className="chart-open" onClick={() => setOpen(true)}>{label}</button>
    {open && <Suspense fallback={null}><ChartView symbol={symbol} initialInterval={interval} asOf={asOf} onClose={() => setOpen(false)} /></Suspense>}
  </>
}

/** RLCD's calibrated call: which action reaches its target before its stop from the latest M15 close. */
export function RlcdBlock({ decision }: { decision: RlcdDecision }) {
  const probabilities = decision.probabilities ?? {}
  const rows: [string, string, number | undefined][] = [['buy', 'BUY', probabilities.buy], ['sell', 'SELL', probabilities.sell], ['hold', 'NEITHER', probabilities.hold]]
  return <div className={`rlcd ${decision.action ?? 'hold'}`}>
    <div className="rlcd-head"><b>RLCD MODEL</b><span className="direction-pill">{(decision.action ?? 'hold').toUpperCase()}</span><em>{decision.tier === 'act' ? 'act' : decision.tier === 'confirm' ? 'wait for the trigger candle' : 'no trade'}</em><small>{decision.model}</small></div>
    <div className="rlcd-bars">{rows.map(([key, label, value]) => <div key={key} className={key}><span>{label}</span><i><u style={{ width: pct(value) }} /></i><small>{pct(value)}</small></div>)}</div>
    <p><span>BREAKEVEN</span>{pct(decision.breakeven_probability)} needed · confidence {decision.confidence?.toFixed(2) ?? '–'}{decision.expected_r ? ` · expected R buy ${decision.expected_r.buy?.toFixed(2) ?? '–'} / sell ${decision.expected_r.sell?.toFixed(2) ?? '–'}` : ''}</p>
    {decision.action && decision.action !== 'hold' && typeof decision.entry === 'number' && <p><span>LEVELS</span>entry {decision.entry} · stop {decision.stop} · targets {decision.targets?.join(' / ')}</p>}
    {decision.warnings?.map((warning, index) => <p key={index} className="rlcd-warning">{warning}</p>)}
  </div>
}

/** Head trader's plan: direction, calibrated probability, levels with reward/risk, timing, scenarios. */
/** Head trader's plan. A WAIT plan shows its range and the two conditional triggers instead of pretending to be a trade. */
export function TradePlanCard({ plan, run, prediction, asOf }: { plan: TradePlan; run: Run; prediction: Prediction | null; asOf?: string }) {
  const neutral = (plan.direction ?? 'neutral') === 'neutral'
  const zones = plan.zones?.filter((item) => item.zone?.length) ?? []
  const ranging = neutral && zones.length > 0
  const band = (values: number[]) => { const low = Math.min(...values), high = Math.max(...values); return low === high ? `${low}` : `${low} – ${high}` }
  const zone = plan.entry_zone?.length ? [Math.min(...plan.entry_zone), Math.max(...plan.entry_zone)] : null
  const entry = zone ? (zone[0] + zone[1]) / 2 : null
  const risk = entry !== null && typeof plan.stop === 'number' ? Math.abs(entry - plan.stop) : null
  const model = run.market?.prediction
  const when = (iso: string) => new Date(iso).toLocaleString([], { year: 'numeric', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' })
  return <div className={`trade-card ${plan.direction ?? 'neutral'}`}>
    {asOf && <div className="replay-banner"><b>REPLAY — NOT A LIVE CALL</b> Decided as of {when(asOf)} UTC{prediction ? `, when price was ${prediction.reference_price.toFixed(2)}` : ''}. All levels below belong to that moment, not to the current market.</div>}
    <div className="trade-head"><span className="direction-pill">{ranging ? 'RANGE' : neutral ? 'WAIT' : plan.direction!.toUpperCase()}</span><b>{plan.symbol}</b><em>{plan.horizon_hours ? `${plan.horizon_hours}h horizon` : ''}</em></div>
    {!asOf && prediction && <p><span>PRICE THEN</span>{prediction.reference_price.toFixed(2)} at {when(prediction.created_at)} UTC</p>}
    {typeof plan.probability === 'number' && <div className="confidence"><i style={{ width: pct(plan.probability) }} /><small>{pct(plan.probability)} {neutral ? 'chance price stays inside the range' : 'chance target 1 is hit before the stop'}</small></div>}
    {plan.needs?.length ? <div className="needs-banner"><b>ADD FOR A FIRMER READ</b>{plan.needs.join(' · ')}</div> : null}
    {plan.timeframes?.length ? <div className="tf-stack">{plan.timeframes.map((frame) => <div key={frame.tf} className={`tf-row ${frame.state ?? ''}`}>
      <b>{frame.tf}</b><em>{(frame.state ?? '–').replace(/_/g, ' ')}</em><p>{frame.read}{frame.source === 'data' ? <small> · from market data</small> : null}</p>
    </div>)}</div> : null}
    {ranging ? <>
      <table className="levels"><tbody>{zone && <tr><th>Range</th><td>{zone[0]} – {zone[1]} <small>· middle of the box = no trade</small></td></tr>}</tbody></table>
      <div className="zone-pair">{zones.map((item, index) => <div key={index} className={`zone ${item.side}`}>
        <b>{item.side === 'sell' ? 'SELL ZONE' : 'BUY ZONE'}</b><strong>{band(item.zone!)}</strong>
        <table className="levels"><tbody>
          {typeof item.stop === 'number' && <tr><th>Stop</th><td>{item.stop}</td></tr>}
          {item.targets?.map((target, row) => <tr key={row}><th>Target {row + 1}</th><td>{target}</td></tr>)}
        </tbody></table>
        {item.trigger && <p><span>TRIGGER</span>{item.trigger}</p>}
        {item.why && <p><span>WHY</span>{item.why}</p>}
      </div>)}</div>
      {zone && <table className="levels"><tbody>
        <tr><th>Breaks up</th><td>body close above {zone[1]}, then retest holds</td></tr>
        <tr><th>Breaks down</th><td>body close below {zone[0]}, then retest holds</td></tr>
      </tbody></table>}
    </> : neutral ? <table className="levels"><tbody>
      {zone && <tr><th>Range</th><td>{zone[0]} – {zone[1]}</td></tr>}
      {zone && <tr><th>Long if</th><td>candle closes above {zone[1]}, then retest holds</td></tr>}
      {zone && <tr><th>Short if</th><td>candle closes below {zone[0]}, then retest holds</td></tr>}
    </tbody></table> : <table className="levels"><tbody>
      {zone ? <tr><th>Entry</th><td>{zone[0] === zone[1] ? zone[0] : `${zone[0]} – ${zone[1]}`}</td></tr> : null}
      {typeof plan.stop === 'number' && <tr><th>Stop</th><td>{plan.stop}</td></tr>}
      {plan.targets?.map((target, index) => <tr key={index}><th>Target {index + 1}</th><td>{target}{risk && entry !== null ? <small> · {(Math.abs(target - entry) / risk).toFixed(1)}R</small> : null}</td></tr>)}
    </tbody></table>}
    {plan.thesis_check?.view && <div className={`thesis ${plan.thesis_check.verdict ?? ''}`}>
      <b>YOUR PREDICTION · {(plan.thesis_check.verdict ?? 'unjudged').replace(/_/g, ' ').toUpperCase()}</b><p>{plan.thesis_check.view}</p>
      {plan.thesis_check.confirms && <p><span>CONFIRMS</span>{plan.thesis_check.confirms}</p>}
      {plan.thesis_check.invalidates && <p><span>INVALIDATES</span>{plan.thesis_check.invalidates}</p>}
    </div>}
    {plan.cases?.length ? <div className="cases">{plan.cases.map((item, index) => <div key={index} className={`case ${item.action}`}>
      <b>{item.action === 'hold' ? 'HOLD' : item.action === 'sell' ? 'SELL' : 'BUY'}</b>
      <div>
        <p className="case-when">{item.when}</p>
        {(item.entry || item.stop || item.targets?.length) ? <p className="case-levels">{item.entry ? <>entry <i>{item.entry}</i></> : null}{item.stop ? <> stop <i>{item.stop}</i></> : null}{item.targets?.length ? <> targets <i>{item.targets.join(' / ')}</i></> : null}</p> : null}
        {item.reason && <p><span>WHY</span>{item.reason}</p>}
        {item.risk && <p><span>RISK</span>{item.risk}</p>}
      </div>
    </div>)}</div> : null}
    {plan.reasons?.length ? <ol className="reasons">{plan.reasons.map((reason, index) => <li key={index}>{reason}</li>)}</ol> : null}
    {plan.continuation?.up && <p><span>TO GO UP</span>{plan.continuation.up}</p>}
    {plan.continuation?.down && <p><span>TO GO DOWN</span>{plan.continuation.down}</p>}
    {plan.timing && <p><span>TIMING</span>{plan.timing}</p>}
    {plan.news?.length ? <div className="news-list"><span>NEWS</span>{plan.news.map((item, index) => <p key={index}><b>{item.time ?? 'time n/a'}</b> {item.event}{item.effect ? ` — ${item.effect}` : ''}</p>)}</div> : null}
    {plan.risks?.length ? <div className="news-list risks"><span>RISKS</span>{plan.risks.map((risk, index) => <p key={index}>{risk}</p>)}</div> : null}
    {plan.key_events?.length ? <div className="event-chips">{plan.key_events.map((event) => <span key={event}>{event}</span>)}</div> : null}
    {plan.scenarios?.length ? <div className="scenarios">{plan.scenarios.map((scenario) => <div key={scenario.name}><b>{scenario.name}</b><i style={{ width: pct(scenario.probability) }} /><small>{pct(scenario.probability)}</small>{scenario.path && <p>{scenario.path}</p>}</div>)}</div> : null}
    {plan.invalidation && <p><span>INVALIDATION</span>{plan.invalidation}</p>}
    {run.market?.rlcd && <RlcdBlock decision={run.market.rlcd} />}
    {model?.prob_up !== undefined && <p className="model-line"><span>ML MODEL</span>P(up) {pct(model.prob_up)}{model.validation?.accuracy !== undefined ? ` · walk-forward accuracy ${pct(model.validation.accuracy)} vs baseline ${pct(model.validation.baseline_accuracy)}` : ''}</p>}
    {prediction && <p className={`prediction-status ${prediction.outcome ?? 'pending'}`}><span>{asOf ? 'WHAT HAPPENED' : 'TRACKING'}</span>{prediction.outcome
      ? `${({ flat: 'stayed in range (wait was right)', target: 'target hit', stop: 'stopped out', correct: 'direction right, target not reached', wrong: 'wrong' } as Record<string, string>)[prediction.outcome] ?? prediction.outcome} · ${prediction.return_pct?.toFixed(2)}% over ${prediction.horizon_hours}h`
      : `scored ${when(prediction.evaluate_at)} UTC`}</p>}
  </div>
}

export function MarketPack({ run }: { run: Run }) {
  const [open, setOpen] = useState(false)
  if (!run.market?.markdown) return null
  return <div className="intent-card"><button className="collapse-toggle" onClick={() => setOpen((value) => !value)}>MARKET DATA PACK {open ? '▾' : '▸'}</button>{open && <Markdown text={run.market.markdown} className="report-inline" />}</div>
}

export function ChartThumbs({ taskId }: { taskId: string }) {
  const [items, setItems] = useState<Attachment[]>([])
  useEffect(() => { void api.attachments(taskId).then(setItems).catch(() => setItems([])) }, [taskId])
  if (!items.length) return null
  return <div className="thumbs inline">{items.map((item) => <a key={item.id} href={attachmentUrl(item.id)} target="_blank" rel="noreferrer noopener" title={item.filename}><img src={attachmentUrl(item.id)} alt={item.filename} /></a>)}</div>
}

/** Project track record: every recorded trade plan and how it scored against the market. */
const WATCH_INTERVALS = ['4h', '1h', '15m'] as const

/** Instruments watched in realtime: on every candle close the structure engine checks the body-close rules. */
function Watchlist({ projectId }: { projectId: string }) {
  const [items, setItems] = useState<WatchItem[]>([])
  const [symbol, setSymbol] = useState('')
  const [intervals, setIntervals] = useState<string[]>(['4h', '1h'])
  const [backtest, setBacktest] = useState<{ key: string; data: Backtest | null; error?: string } | null>(null)
  const load = () => void api.watchlist(projectId).then(setItems).catch(() => setItems([]))
  const [retest, setRetest] = useState<{ symbol: string; pip: number; sl: number; tp: number; loading: boolean; markdown?: string; lines?: { level: number; level_type: string; level_interval: string; status: string; distance_pips?: number }[]; error?: string } | null>(null)
  async function runRetest(symbol: string, pip: number, sl: number, tp: number) {
    setRetest({ symbol, pip, sl, tp, loading: true })
    try { const data = await api.retest(symbol, pip, sl, tp); setRetest({ symbol, pip, sl, tp, loading: false, markdown: data.markdown, lines: data.active }) } catch (caught) { setRetest({ symbol, pip, sl, tp, loading: false, error: caught instanceof Error ? caught.message : String(caught) }) }
  }
  const [topDown, setTopDown] = useState<{ symbol: string; markdown?: string; error?: string } | null>(null)
  async function runTopDown(symbol: string, pip: number) {
    setTopDown({ symbol })
    try { setTopDown({ symbol, markdown: (await api.mtf(symbol, pip, 20, 50)).markdown ?? 'No read returned.' }) } catch (caught) { setTopDown({ symbol, error: caught instanceof Error ? caught.message : String(caught) }) }
  }
  const [quick, setQuick] = useState<{ symbol: string; decision?: RlcdDecision; error?: string } | null>(null)
  async function runQuick(symbol: string) {
    setQuick({ symbol })
    try { setQuick({ symbol, decision: await api.rlcdDecide(symbol) }) } catch (caught) { setQuick({ symbol, error: caught instanceof Error ? caught.message : String(caught) }) }
  }
  const [lines, setLines] = useState<{ symbol: string; markdown?: string; error?: string } | null>(null)
  async function runLines(symbol: string) {
    setLines({ symbol })
    try { setLines({ symbol, markdown: (await api.levels(symbol)).markdown ?? 'No lines returned.' }) } catch (caught) { setLines({ symbol, error: caught instanceof Error ? caught.message : String(caught) }) }
  }
  async function runBacktest(symbol: string, interval: string) {
    const key = `${symbol} ${interval}`
    setBacktest({ key, data: null })
    try { setBacktest({ key, data: await api.backtest(symbol, interval) }) } catch (caught) { setBacktest({ key, data: null, error: caught instanceof Error ? caught.message : String(caught) }) }
  }
  useEffect(load, [projectId])
  async function add(event: FormEvent) {
    event.preventDefault()
    if (!symbol.trim()) return
    await api.addWatch(projectId, symbol, intervals).catch(() => undefined)
    setSymbol('')
    load()
  }
  return <div className="watchlist">
    <form onSubmit={(event) => void add(event)}>
      <input value={symbol} onChange={(event) => setSymbol(event.target.value)} placeholder="Watch USDJPY, XAUUSD…" aria-label="Symbol to watch" />
      {WATCH_INTERVALS.map((interval) => <button type="button" key={interval} className={intervals.includes(interval) ? 'active' : ''} onClick={() => setIntervals((current) => current.includes(interval) ? current.filter((item) => item !== interval) : [...current, interval])}>{interval}</button>)}
      <button type="submit" aria-label="Add to watchlist"><Plus size={12} /></button>
    </form>
    {items.map((item) => <div key={item.id} className="watch-row" title={item.last_error ?? (item.last_checked_at ? `checked ${timeLabel(item.last_checked_at)}` : 'waiting for first check')}>
      <i className={`roster-dot ${item.last_error ? 'failed' : item.last_checked_at ? 'running' : 'pending'}`} /><b>{item.symbol}</b>
      <small>{item.intervals.map((interval) => <button key={interval} className="backtest-link" onClick={() => void runBacktest(item.symbol, interval)} title={`Backtest the structure rules on ${item.symbol} ${interval}`}>{interval} ⟲</button>)}<button className="backtest-link" onClick={() => void runRetest(item.symbol, item.symbol.includes('XAU') ? 0.1 : item.symbol.includes('JPY') ? 0.01 : 0.0001, 20, 50)} title="Retest lab: break → retest → continue with fixed-pip stop and target">retest</button><ChartButton symbol={item.symbol} label="chart" /><button className="backtest-link" onClick={() => void runQuick(item.symbol)} title="RLCD: calibrated buy / sell / hold from the latest M15 close, stop 20 pips, targets 50 and 100 pips — no agents, answers in seconds">rlcd</button><button className="backtest-link" onClick={() => void runTopDown(item.symbol, item.symbol.includes('XAU') ? 0.1 : item.symbol.includes('JPY') ? 0.01 : 0.0001)} title="Top-down read: H4 context → H1 setup → M15/M5 trigger, range or break, sell zone / buy zone">top-down</button><button className="backtest-link" onClick={() => void runLines(item.symbol)} title="Lines and reactions: zones, touches, hold-vs-break odds, M15 → H1 → H4">lines</button></small>
      <button onClick={() => void api.deleteWatch(item.id).then(load)} aria-label={`Stop watching ${item.symbol}`}><X size={11} /></button>
    </div>)}
    {quick && <div className="backtest-card">
      <div className="backtest-head"><b>RLCD · {quick.symbol} M15</b><button onClick={() => setQuick(null)} aria-label="Close"><X size={11} /></button></div>
      {!quick.decision && !quick.error && <p className="empty">Asking the model…</p>}
      {quick.error && <p className="run-error">{quick.error}</p>}
      {quick.decision && <RlcdBlock decision={quick.decision} />}
    </div>}
    {topDown && <div className="backtest-card">
      <div className="backtest-head"><b>TOP-DOWN · {topDown.symbol}</b><button onClick={() => setTopDown(null)} aria-label="Close"><X size={11} /></button></div>
      {!topDown.markdown && !topDown.error && <p className="empty">Reading H4, then H1, then M15 and M5…</p>}
      {topDown.error && <p className="run-error">{topDown.error}</p>}
      {topDown.markdown && <Markdown text={topDown.markdown} className="report-inline" />}
    </div>}
    {lines && <div className="backtest-card">
      <div className="backtest-head"><b>LINES & REACTIONS · {lines.symbol}</b><button onClick={() => setLines(null)} aria-label="Close"><X size={11} /></button></div>
      {!lines.markdown && !lines.error && <p className="empty">Finding your lines and every reaction to them…</p>}
      {lines.error && <p className="run-error">{lines.error}</p>}
      {lines.markdown && <Markdown text={lines.markdown} className="report-inline" />}
    </div>}
    {retest && <div className="backtest-card no-edge">
      <div className="backtest-head"><b>RETEST LAB · {retest.symbol} M15</b><button onClick={() => setRetest(null)} aria-label="Close"><X size={11} /></button></div>
      <form className="retest-form" onSubmit={(event) => { event.preventDefault(); const form = new FormData(event.currentTarget); void runRetest(retest.symbol, Number(form.get('pip')) || retest.pip, Number(form.get('sl')) || retest.sl, Number(form.get('tp')) || retest.tp) }}>
        <label>pip<input name="pip" defaultValue={retest.pip} /></label><label>stop<input name="sl" defaultValue={retest.sl} /></label><label>target<input name="tp" defaultValue={retest.tp} /></label><button type="submit">RUN</button>
      </form>
      {retest.loading && <p className="empty">Replaying every break → retest in the history…</p>}
      {retest.error && <p className="run-error">{retest.error}</p>}
      {retest.lines && retest.lines.length > 0 && <table className="options"><thead><tr><th>Your lines now</th><th>status</th><th>pips away</th></tr></thead><tbody>
        {retest.lines.slice(0, 8).map((line, index) => <tr key={index}><td>{line.level.toFixed(2)} <small>{line.level_type} · {line.level_interval}</small></td><td>{line.status.replace(/_/g, ' ')}</td><td>{line.distance_pips?.toFixed(0) ?? '–'}</td></tr>)}
      </tbody></table>}
      {retest.markdown && <Markdown text={retest.markdown} className="report-inline" />}
    </div>}
    {backtest && <BacktestCard title={backtest.key} data={backtest.data} error={backtest.error} onClose={() => setBacktest(null)} />}
  </div>
}

const pctOf = (value: number | null | undefined) => typeof value === 'number' ? `${(value * 100).toFixed(1)}%` : '–'
const rOf = (value: number | null | undefined) => typeof value === 'number' ? `${value >= 0 ? '+' : ''}${value.toFixed(3)}R` : '–'

/** Proof before prediction: how the body-close rules performed on history, net of costs. */
function BacktestCard({ title, data, error, onClose }: { title: string; data: Backtest | null; error?: string; onClose: () => void }) {
  if (error) return <div className="backtest-card"><div className="backtest-head"><b>BACKTEST · {title}</b><button onClick={onClose} aria-label="Close"><X size={11} /></button></div><p className="run-error">{error}</p></div>
  if (!data) return <div className="backtest-card"><div className="backtest-head"><b>BACKTEST · {title}</b></div><p className="empty">Walking the history…</p></div>
  const overall = data.overall
  const edge = overall.verdict === 'edge'
  const rows = (group?: Record<string, BacktestStats>) => Object.entries(group ?? {}).filter(([, stats]) => (stats.n_trades ?? 0) > 0)
  const curve = data.equity_curve ?? []
  const values = curve.map((point) => point.cum_r)
  const [low, high] = [Math.min(0, ...values), Math.max(0, ...values)]
  const path = values.map((value, index) => `${index === 0 ? 'M' : 'L'} ${(index / Math.max(values.length - 1, 1)) * 100} ${30 - ((value - low) / Math.max(high - low, 1e-9)) * 30}`).join(' ')
  return <div className={`backtest-card ${edge ? 'edge' : 'no-edge'}`}>
    <div className="backtest-head"><b>BACKTEST · {title}</b><span className="verdict">{(overall.verdict ?? 'unknown').toUpperCase()}</span><button onClick={onClose} aria-label="Close"><X size={11} /></button></div>
    <div className="track-summary">
      <span><small>TRADES</small><b>{overall.n_trades ?? 0}</b></span>
      <span title={`95% CI ${overall.win_rate_ci95?.map(pctOf).join(' – ') ?? '–'}`}><small>WIN RATE</small><b>{pctOf(overall.win_rate)}</b></span>
      <span title="Win rate needed to break even after costs"><small>BREAKEVEN</small><b>{pctOf(overall.breakeven_win_rate)}</b></span>
      <span title="Average result per trade in risk units, net of costs"><small>EXPECTANCY</small><b>{rOf(overall.expectancy_r)}</b></span>
    </div>
    {values.length > 1 && <svg className="equity" viewBox="0 0 100 30" preserveAspectRatio="none" aria-label="Equity curve in R"><path d={path} /></svg>}
    <table className="options"><thead><tr><th>By session</th><th>n</th><th>win</th><th>exp.</th></tr></thead><tbody>
      {rows(data.by_session).map(([name, stats]) => <tr key={name}><td>{name}</td><td>{stats.n_trades}</td><td>{pctOf(stats.win_rate)}</td><td>{rOf(stats.expectancy_r)}</td></tr>)}
    </tbody></table>
    <table className="options"><thead><tr><th>By signal</th><th>n</th><th>win</th><th>exp.</th></tr></thead><tbody>
      {rows(data.by_type).map(([name, stats]) => <tr key={name}><td>{name.replace(/_/g, ' ')}</td><td>{stats.n_trades}</td><td>{pctOf(stats.win_rate)}</td><td>{rOf(stats.expectancy_r)}</td></tr>)}
    </tbody></table>
    <p className="backtest-note">Total {rOf(overall.total_r)} · profit factor {overall.profit_factor?.toFixed(2) ?? '–'} · max drawdown {overall.max_drawdown_r?.toFixed(1) ?? '–'}R. "Edge" requires the win-rate confidence interval to clear breakeven, positive expectancy and ≥100 trades.</p>
  </div>
}

export function PredictionsPanel({ book, projectId }: { book: PredictionBook | null; projectId: string }) {
  if (!book) return <div className="empty">Loading track record…</div>
  const { summary, items } = book
  return <div className="memory-panel">
    <Watchlist projectId={projectId} />
    <div className="track-summary">
      <span><small>SCORED</small><b>{summary.scored}/{summary.total}</b></span>
      <span title="Target reached before stop (or range held for neutral calls)"><small>HIT RATE</small><b>{pct(summary.hit_rate)}</b></span>
      <span title="Direction right at the horizon"><small>DIRECTION</small><b>{pct(summary.direction_rate)}</b></span>
      <span><small>BRIER</small><b>{summary.brier?.toFixed(3) ?? '–'}</b></span>
    </div>
    <div className="memory-list">
      {items.length === 0 && <div className="empty">No predictions yet. Trading desk runs record their trade plan here and score it automatically after the horizon.</div>}
      {items.map((item) => <div key={item.id} className="memory-item">
        <span className={`memory-kind outcome-${item.outcome ?? 'pending'}`}>{item.outcome ?? 'pending'}</span>
        <p><b>{item.symbol}</b> {item.direction} · {pct(item.probability)} · {item.horizon_hours}h · from {item.reference_price}{item.return_pct !== null ? ` · ${item.return_pct.toFixed(2)}%` : ''}</p>
        <a>{timeLabel(item.created_at)} → {new Date(item.evaluate_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })}{item.brier !== null ? ` · Brier ${item.brier.toFixed(3)}` : ''}</a>
      </div>)}
    </div>
  </div>
}

