import { useEffect, useMemo, useRef, useState } from 'react'
import { X } from 'lucide-react'
import { api } from '../api'
import type { ChartData, RlcdDecision } from '../types'
import { RlcdBlock } from './Trading'

const INTERVALS = ['5m', '15m', '1h', '4h', '1d'] as const
const LAYERS = [['zones', 'zones'], ['swings', 'HH HL LH LL'], ['boxes', 'consolidation'], ['events', 'sweeps & breaks'], ['patterns', 'patterns'], ['sessions', 'sessions'], ['news', 'news'], ['plan', 'plan']] as const
type Layer = typeof LAYERS[number][0]
const EVENT_MARK: Record<string, [string, string]> = {
  sweep_high: ['S', 'sweep'], sweep_low: ['S', 'sweep'], fakeout_up: ['F', 'fakeout'], fakeout_down: ['F', 'fakeout'],
  break_up: ['B', 'break'], break_down: ['B', 'break'], retest_hold: ['R', 'retest'], retest_fail: ['R', 'retest'],
}
const PAD = { top: 18, right: 86, bottom: 22, left: 8 }
const FUTURE_BARS = 14

/** View Engine's own chart of an instrument with everything it reads drawn on the candles, plus the reading. */
export default function ChartView({ symbol: initialSymbol, initialInterval = '15m', asOf, onClose }: { symbol: string; initialInterval?: string; asOf?: string; onClose: () => void }) {
  const [symbol, setSymbol] = useState(initialSymbol)
  const [bars, setBars] = useState(160)
  const [interval, setIntervalValue] = useState(INTERVALS.includes(initialInterval as typeof INTERVALS[number]) ? initialInterval : '15m')
  const [data, setData] = useState<ChartData | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  const [layers, setLayers] = useState<Record<Layer, boolean>>({ zones: true, swings: true, boxes: true, events: true, patterns: false, sessions: true, news: true, plan: true })
  const [decision, setDecision] = useState<RlcdDecision | null>(null)
  const [hover, setHover] = useState<number | null>(null)
  const [width, setWidth] = useState(900)
  const [height, setHeight] = useState(470)
  const frame = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const element = frame.current
    if (!element) return
    const observer = new ResizeObserver(([entry]) => { setWidth(Math.max(320, Math.floor(entry.contentRect.width))); setHeight(Math.max(360, Math.floor(entry.contentRect.height) - 44)) })
    observer.observe(element)
    return () => observer.disconnect()
  }, [])

  // Live charts refresh on their own; a replay (asOf) is a fixed moment.
  useEffect(() => {
    let cancelled = false
    const load = (first: boolean) => {
      if (first) { setLoading(true); setData(null) }
      api.chart(symbol, interval, asOf, bars).then((value) => { if (!cancelled) { setData(value); setError('') } }).catch((caught) => { if (!cancelled) setError(caught instanceof Error ? caught.message : String(caught)) }).finally(() => { if (!cancelled) setLoading(false) })
    }
    load(true)
    const timer = asOf ? null : window.setInterval(() => load(false), 30000)
    return () => { cancelled = true; if (timer) window.clearInterval(timer) }
  }, [symbol, interval, asOf, bars])

  useEffect(() => {
    setDecision(null)
    if (asOf || !['5m', '15m', '1h'].includes(interval)) return
    let cancelled = false
    api.rlcdDecide(symbol, undefined, interval).then((value) => { if (!cancelled) setDecision(value) }).catch(() => undefined)
    return () => { cancelled = true }
  }, [symbol, interval, asOf, data?.candles.length])

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => { if (event.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const geometry = useMemo(() => {
    const candles = data?.candles ?? []
    if (!candles.length) return null
    const plot = { width: width - PAD.left - PAD.right, height: height - PAD.top - PAD.bottom }
    const step = plot.width / (candles.length + FUTURE_BARS)
    let low = Math.min(...candles.map((candle) => candle.l)), high = Math.max(...candles.map((candle) => candle.h))
    // Plan levels belong on screen when they are near the visible range, not when they would flatten the candles.
    const span = high - low || 1
    const near = (value: unknown): value is number => typeof value === 'number' && value > low - span * 0.35 && value < high + span * 0.35
    const planLevels = (data?.playbook?.cases ?? []).flatMap((item) => [item.entry_price, item.stop, ...(item.targets ?? [])]).filter(near)
    low = Math.min(low, ...planLevels); high = Math.max(high, ...planLevels)
    const margin = (high - low) * 0.06 || 1
    low -= margin; high += margin
    const times = candles.map((candle) => Date.parse(candle.t))
    const y = (price: number) => PAD.top + (high - price) / (high - low) * plot.height
    const x = (index: number) => PAD.left + (index + 0.5) * step
    // Fractional index of any timestamp, extrapolated past the last candle with the bar spacing.
    const indexOf = (iso: string | null | undefined) => {
      if (!iso) return candles.length - 1
      const time = Date.parse(iso)
      if (time <= times[0]) return 0
      const last = times.length - 1
      if (time >= times[last]) { const bar = last > 0 ? times[last] - times[last - 1] : 60000; return Math.min(last + (time - times[last]) / bar, last + FUTURE_BARS - 1) }
      let a = 0, b = last
      while (b - a > 1) { const mid = (a + b) >> 1; if (times[mid] <= time) a = mid; else b = mid }
      return a + (time - times[a]) / (times[b] - times[a])
    }
    const ticks = Array.from({ length: 7 }, (_, index) => low + (high - low) * index / 6)
    return { candles, plot, step, low, high, y, x, indexOf, ticks, right: PAD.left + plot.width }
  }, [data, width, height])

  // A readable chart shows the few things that matter now, not everything ever detected.
  const visibleZones = useMemo(() => {
    if (!geometry || !data?.zones) return []
    const inView = data.zones.filter((zone) => zone.level > geometry.low && zone.level < geometry.high)
    const pick = (role: string) => inView.filter((zone) => zone.role === role).sort((a, b) => Math.abs(a.level - data.price) - Math.abs(b.level - data.price)).slice(0, 3)
    return [...pick('resistance'), ...pick('support')]
  }, [geometry, data])
  const visibleBoxes = useMemo(() => {
    const boxes = data?.boxes ?? []
    return [...boxes.filter((box) => box.interval !== interval), ...boxes.filter((box) => box.interval === interval).slice(-3)]
  }, [data, interval])
  const visibleEvents = useMemo(() => (data?.events ?? []).filter((event) => EVENT_MARK[event.type]).slice(-14), [data])

  const digits = data ? (data.price > 500 ? 2 : data.price > 20 ? 3 : 5) : 2
  const fmt = (value: number | null | undefined) => typeof value === 'number' ? value.toFixed(digits) : '–'
  const clock = (iso: string) => new Date(iso).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false, timeZone: 'UTC' })
  const reading = data?.reading
  const shown = hover !== null && geometry ? geometry.candles[hover] : geometry?.candles[geometry.candles.length - 1]

  return <div className="chart-layer" role="dialog" aria-label={`${symbol} chart`}>
    <div className="chart-shell">
      <header className="chart-top">
        <form onSubmit={(event) => { event.preventDefault(); const next = String(new FormData(event.currentTarget).get('symbol') ?? '').trim().toUpperCase(); if (next) setSymbol(next) }}><input name="symbol" key={symbol} defaultValue={symbol} aria-label="Instrument" spellCheck={false} /></form>
        <div className="segmented">{INTERVALS.map((value) => <button type="button" key={value} className={interval === value ? 'active' : ''} onClick={() => setIntervalValue(value)}>{value}</button>)}</div>
        <div className="segmented">{([80, 160, 300] as const).map((value) => <button type="button" key={value} className={bars === value ? 'active' : ''} onClick={() => setBars(value)} title={`Show the last ${value} candles`}>{value}</button>)}</div>
        <span className="chart-ohlc">{shown ? <>{clock(shown.t)} UTC · O {fmt(shown.o)} H {fmt(shown.h)} L {fmt(shown.l)} C {fmt(shown.c)}{shown.forming ? ' · forming' : ''}</> : null}</span>
        <em>{asOf ? `replay as of ${clock(asOf)} UTC` : 'live · refreshes every 30 s'}</em>
        <button type="button" className="chart-close" onClick={onClose} aria-label="Close chart"><X size={14} /></button>
      </header>
      <div className="chart-layers">{LAYERS.map(([id, label]) => <button type="button" key={id} className={layers[id] ? 'active' : ''} onClick={() => setLayers((current) => ({ ...current, [id]: !current[id] }))}>{label}</button>)}</div>
      <div className="chart-body">
        <div className="chart-frame" ref={frame}>
          {loading && <p className="empty">Drawing {symbol} {interval}…</p>}
          {error && <p className="run-error">{error}</p>}
          {geometry && data && <svg width={width} height={height} onMouseLeave={() => setHover(null)} onMouseMove={(event) => { const box = event.currentTarget.getBoundingClientRect(); const index = Math.floor((event.clientX - box.left - PAD.left) / geometry.step); setHover(index >= 0 && index < geometry.candles.length ? index : null) }}>
            {layers.sessions && data.sessions?.map((session, index) => { const a = geometry.x(geometry.indexOf(session.start)) - geometry.step / 2, b = geometry.x(geometry.indexOf(session.end)) + geometry.step / 2; return b > a ? <g key={`s${index}`}><rect x={a} y={PAD.top} width={b - a} height={geometry.plot.height} className={`sess ${session.name.toLowerCase().replace(/\s+/g, '')}`} /><text x={a + 3} y={height - 8} className="sess-label">{session.name}</text></g> : null })}
            {geometry.ticks.map((tick) => <g key={tick}><line x1={PAD.left} x2={geometry.right} y1={geometry.y(tick)} y2={geometry.y(tick)} className="grid" /><text x={geometry.right + 5} y={geometry.y(tick) + 3} className="axis">{fmt(tick)}</text></g>)}
            {layers.zones && visibleZones.map((zone, index) => { const top = geometry.y(Math.min(zone.high ?? zone.level, geometry.high)), bottom = geometry.y(Math.max(zone.low ?? zone.level, geometry.low)); return <g key={`z${index}`} className={`zone-band ${zone.role}`}>
              <title>{zone.role} {fmt(zone.level)} · seen on {(zone.timeframes ?? []).join(' + ')} · {zone.touches ?? 0} touches</title>
              <rect x={PAD.left} y={top} width={geometry.plot.width} height={Math.max(2, bottom - top)} />
              <text x={geometry.right - 4} y={top - 3} textAnchor="end">{zone.role === 'resistance' ? 'R' : 'S'} {fmt(zone.level)} · {(zone.timeframes ?? []).join('+')} · {zone.touches ?? 0}×</text>
            </g> })}
            {layers.boxes && visibleBoxes.map((box, index) => { const a = geometry.x(geometry.indexOf(box.start)) - geometry.step / 2, b = geometry.x(geometry.indexOf(box.end ?? null)) + geometry.step / 2; const top = geometry.y(Math.min(box.top, geometry.high)), bottom = geometry.y(Math.max(box.bottom, geometry.low)); return bottom > top && b > a ? <g key={`b${index}`} className={`cons-box ${box.state} ${box.interval === interval ? 'own' : 'higher'}`}>
              <rect x={a} y={top} width={b - a} height={bottom - top} />
              <text x={a + 4} y={top + 11}>{box.interval} box {fmt(box.bottom)}–{fmt(box.top)}{box.state === 'active' ? '' : box.state === 'broken_up' ? ' · broken up' : ' · broken down'}</text>
            </g> : null })}
            {data.impulses?.map((impulse, index) => <line key={`i${index}`} className={`impulse ${impulse.direction}`} x1={geometry.x(geometry.indexOf(impulse.start))} y1={geometry.y(impulse.from)} x2={geometry.x(geometry.indexOf(impulse.end))} y2={geometry.y(impulse.to)} />)}
            {geometry.candles.map((candle, index) => { const cx = geometry.x(index), bodyTop = geometry.y(Math.max(candle.o, candle.c)), bodyBottom = geometry.y(Math.min(candle.o, candle.c)), bodyWidth = Math.max(1, geometry.step * 0.68); return <g key={candle.t} className={`candle ${candle.c >= candle.o ? 'up' : 'down'}${candle.forming ? ' forming' : ''}`}>
              <line x1={cx} x2={cx} y1={geometry.y(candle.h)} y2={geometry.y(candle.l)} />
              <rect x={cx - bodyWidth / 2} y={bodyTop} width={bodyWidth} height={Math.max(1, bodyBottom - bodyTop)} />
            </g> })}
            {layers.patterns && (data.patterns ?? []).filter((pattern) => (pattern.points?.length ?? 0) > 1).slice(-3).map((pattern, index) => { const points = pattern.points!.map((point) => `${geometry.x(geometry.indexOf(point.t))},${geometry.y(point.price)}`); const last = pattern.points![pattern.points!.length - 1]; return <g key={`p${index}`} className={`pattern ${pattern.direction ?? ''}`}>
              <polyline points={points.join(' ')} />
              <text x={geometry.x(geometry.indexOf(last.t)) + 4} y={geometry.y(last.price) - 4}>{pattern.name.replace(/_/g, ' ')}{pattern.state === 'forming' ? ' (forming)' : ''}</text>
            </g> })}
            {layers.patterns && (data.patterns ?? []).filter((pattern) => pattern.points?.length === 1).slice(-8).map((pattern, index) => { const point = pattern.points![0]; return <text key={`k${index}`} className="candle-pattern" x={geometry.x(geometry.indexOf(point.t))} y={geometry.y(point.price) + 22} textAnchor="middle"><title>{pattern.text ?? pattern.name}</title>{pattern.name}</text> })}
            {layers.swings && data.swings?.map((swing, index) => { const sx = geometry.x(geometry.indexOf(swing.t)), high = swing.kind === 'high'; return <text key={`w${index}`} x={sx} y={geometry.y(swing.price) + (high ? -6 : 13)} className={`swing ${swing.label}`} textAnchor="middle">{swing.label}</text> })}
            {layers.events && visibleEvents.map((event, index) => { const ex = geometry.x(geometry.indexOf(event.t)), up = /high|up/.test(event.type), ey = geometry.y(event.price) + (up ? -16 : 16); return <g key={`e${index}`} className={`mark ${EVENT_MARK[event.type][1]}`}><title>{event.text ?? event.type}</title><circle cx={ex} cy={ey} r={6.5} /><text x={ex} y={ey + 3} textAnchor="middle">{EVENT_MARK[event.type][0]}</text></g> })}
            {layers.news && data.news?.map((item, index) => { const nx = geometry.x(geometry.indexOf(item.t)); return <g key={`n${index}`} className={`news ${item.past ? 'past' : 'next'}`}><title>{item.title} ({item.currency}) · {clock(item.t)} UTC</title><line x1={nx} x2={nx} y1={PAD.top} y2={PAD.top + geometry.plot.height} /><text x={nx + 3} y={PAD.top + 9}>{(item.type || 'news').toUpperCase()}</text></g> })}
            {layers.plan && data.playbook?.cases?.filter((item) => item.action !== 'hold' && typeof item.entry_price === 'number').slice(0, 1).flatMap((item, index) => {
              const start = geometry.x(geometry.candles.length - 1), lines: [string, number, string][] = [['entry', item.entry_price as number, `${item.action.toUpperCase()} ${fmt(item.entry_price)}`]]
              if (typeof item.stop === 'number') lines.push(['stop', item.stop, `stop ${fmt(item.stop)}`])
              item.targets?.slice(0, 2).forEach((target, row) => lines.push(['target', target, `T${row + 1} ${fmt(target)}`]))
              return lines.filter(([, value]) => value > geometry.low && value < geometry.high).map(([kind, value, label]) => <g key={`c${index}${label}`} className={`plan-line ${kind} ${item.action}`}><line x1={start} x2={geometry.right} y1={geometry.y(value)} y2={geometry.y(value)} /><text x={geometry.right - 4} y={geometry.y(value) - 3} textAnchor="end">{label}</text></g>)
            })}
            <g className="price-line"><line x1={PAD.left} x2={geometry.right} y1={geometry.y(data.price)} y2={geometry.y(data.price)} /><rect x={geometry.right + 1} y={geometry.y(data.price) - 8} width={PAD.right - 4} height={16} /><text x={geometry.right + 5} y={geometry.y(data.price) + 3}>{fmt(data.price)}</text></g>
            {hover !== null && <line className="cross" x1={geometry.x(hover)} x2={geometry.x(hover)} y1={PAD.top} y2={PAD.top + geometry.plot.height} />}
          </svg>}
          <div className="chart-legend"><span className="mark sweep">S</span> wick sweep <span className="mark fakeout">F</span> fakeout <span className="mark break">B</span> body break <span className="mark retest">R</span> retest · labels are body swings <span title={data?.data_note ?? ''}>· {(data?.data_note ?? '').slice(0, 110)}{(data?.data_note ?? '').length > 110 ? '…' : ''}</span></div>
        </div>
        <aside className="chart-reading">
          {reading ? <>
            <div className={`risk-pill ${reading.risk?.level ?? 'medium'}`}>RISK {(reading.risk?.level ?? '–').toUpperCase()}</div>
            {reading.risk?.note && <small className="risk-note">{reading.risk.note}</small>}
            <h4>{(reading.phase ?? reading.regime ?? '').replace(/_/g, ' ')}</h4>
            <p>{reading.summary}</p>
            {data?.higher && <table className="levels"><tbody>{Object.entries(data.higher).map(([frame, value]) => <tr key={frame}><th>{frame}</th><td>{(value.regime ?? '–').replace(/_/g, ' ')}{value.box ? ` · box ${fmt(value.box.bottom)}–${fmt(value.box.top)}` : ''}</td></tr>)}</tbody></table>}
            {reading.wait_for?.length ? <><span className="reading-label">WAIT FOR</span><ol>{reading.wait_for.map((item, index) => <li key={index}>{item}</li>)}</ol></> : null}
            {reading.what_next?.length ? <><span className="reading-label">WHAT HAPPENS NEXT</span><ul>{reading.what_next.map((item, index) => <li key={index}>{item}</li>)}</ul></> : null}
            {reading.risk?.reasons?.length ? <><span className="reading-label">WHY THIS RISK</span><ul className="risk-reasons">{reading.risk.reasons.map((item, index) => <li key={index}>{item}</li>)}</ul></> : null}
            {data?.playbook?.cases?.length ? <><span className="reading-label">IF–THEN</span><div className="cases">{data.playbook.cases.map((item, index) => <div key={index} className={`case ${item.action}`}><b>{item.action.toUpperCase()}</b><div><p className="case-when">{item.when}</p>{item.entry_price ? <p className="case-levels">entry <i>{fmt(item.entry_price)}</i>{item.stop ? <> stop <i>{fmt(item.stop)}</i></> : null}{item.targets?.length ? <> targets <i>{item.targets.map(fmt).join(' / ')}</i></> : null}</p> : null}{item.risk && <p><span>RISK</span>{item.risk}</p>}</div></div>)}</div></> : null}
          </> : !loading && !error ? <p className="empty">No reading returned.</p> : null}
          {decision && <RlcdBlock decision={decision} />}
        </aside>
      </div>
    </div>
  </div>
}
