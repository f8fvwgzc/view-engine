import { FormEvent, useEffect, useState } from 'react'
import { Plus, X } from 'lucide-react'
import { api, attachmentUrl } from '../api'
import { timeLabel, type Attachment, type Prediction, type PredictionBook, type Run, type TradePlan, type WatchItem } from '../types'
import { Markdown } from './Markdown'

const pct = (value: number | null | undefined) => typeof value === 'number' ? `${Math.round(value * 100)}%` : '–'

/** Head trader's plan: direction, calibrated probability, levels with reward/risk, timing, scenarios. */
export function TradePlanCard({ plan, run, prediction }: { plan: TradePlan; run: Run; prediction: Prediction | null }) {
  const entry = plan.entry_zone?.length ? (Math.min(...plan.entry_zone) + Math.max(...plan.entry_zone)) / 2 : null
  const risk = entry !== null && typeof plan.stop === 'number' ? Math.abs(entry - plan.stop) : null
  const model = run.market?.prediction
  return <div className={`trade-card ${plan.direction ?? 'neutral'}`}>
    <div className="trade-head"><span className="direction-pill">{(plan.direction ?? 'neutral').toUpperCase()}</span><b>{plan.symbol}</b><em>{plan.horizon_hours ? `${plan.horizon_hours}h horizon` : ''}</em></div>
    {typeof plan.probability === 'number' && <div className="confidence"><i style={{ width: pct(plan.probability) }} /><small>{pct(plan.probability)} desk probability</small></div>}
    <table className="levels"><tbody>
      {plan.entry_zone?.length ? <tr><th>Entry</th><td>{plan.entry_zone.join(' – ')}</td></tr> : null}
      {typeof plan.stop === 'number' && <tr><th>Stop</th><td>{plan.stop}</td></tr>}
      {plan.targets?.map((target, index) => <tr key={index}><th>Target {index + 1}</th><td>{target}{risk && entry !== null ? <small> · {(Math.abs(target - entry) / risk).toFixed(1)}R</small> : null}</td></tr>)}
    </tbody></table>
    {plan.timing && <p><span>TIMING</span>{plan.timing}</p>}
    {plan.key_events?.length ? <div className="event-chips">{plan.key_events.map((event) => <span key={event}>{event}</span>)}</div> : null}
    {plan.scenarios?.length ? <div className="scenarios">{plan.scenarios.map((scenario) => <div key={scenario.name}><b>{scenario.name}</b><i style={{ width: pct(scenario.probability) }} /><small>{pct(scenario.probability)}</small>{scenario.path && <p>{scenario.path}</p>}</div>)}</div> : null}
    {plan.invalidation && <p><span>INVALIDATION</span>{plan.invalidation}</p>}
    {model?.prob_up !== undefined && <p className="model-line"><span>ML MODEL</span>P(up) {pct(model.prob_up)}{model.validation?.accuracy !== undefined ? ` · walk-forward accuracy ${pct(model.validation.accuracy)} vs baseline ${pct(model.validation.baseline_accuracy)}` : ''}</p>}
    {prediction && <p className={`prediction-status ${prediction.outcome ?? 'pending'}`}><span>TRACKING</span>{prediction.outcome
      ? `${prediction.outcome.toUpperCase()} · ${prediction.return_pct?.toFixed(2)}% · Brier ${prediction.brier?.toFixed(3)}`
      : `recorded at ${prediction.reference_price} · scored ${new Date(prediction.evaluate_at).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false })}`}</p>}
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
  const load = () => void api.watchlist(projectId).then(setItems).catch(() => setItems([]))
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
      <i className={`roster-dot ${item.last_error ? 'failed' : item.last_checked_at ? 'running' : 'pending'}`} /><b>{item.symbol}</b><small>{item.intervals.join(' · ')}</small>
      <button onClick={() => void api.deleteWatch(item.id).then(load)} aria-label={`Stop watching ${item.symbol}`}><X size={11} /></button>
    </div>)}
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

