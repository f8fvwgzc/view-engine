import { FormEvent, ReactNode, useEffect, useMemo, useState } from 'react'
import { Check, RefreshCw, Save, X } from 'lucide-react'
import { api } from '../api'
import { ACCENTS, DEFAULT_APPEARANCE, MONO_FONTS, SANS_FONTS, applyAppearance, loadAppearance, type Appearance } from '../appearance'
import { ROUTE_ROLES, type MarketSymbol, type Project, type Provider, type Settings, type Skill, type SkillImport, type TaskConfig } from '../types'
import { Markdown } from './Markdown'

function useEscape(onEscape: () => void) {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => { if (event.key === 'Escape') onEscape() }
    window.addEventListener('keydown', handler)
    return () => window.removeEventListener('keydown', handler)
  }, [onEscape])
}

function Modal({ onClose, wide, children }: { onClose: () => void; wide?: boolean; children: ReactNode }) {
  useEscape(onClose)
  return <div className="modal-layer" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <div className={`modal ${wide ? 'wide' : ''}`}><button type="button" className="modal-close" onClick={onClose} aria-label="Close"><X size={17} /></button>{children}</div>
  </div>
}

export function ProjectModal({ onClose, onCreate }: { onClose: () => void; onCreate: (name: string, description: string) => void }) {
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    onCreate(String(form.get('name') ?? ''), String(form.get('description') ?? ''))
  }
  return <Modal onClose={onClose}><form onSubmit={submit}>
    <p className="eyebrow">NEW PROJECT</p><h2>Create project</h2>
    <label>NAME<input name="name" placeholder="Japan market entry" required autoFocus /></label>
    <label>CONTEXT <small>(optional — who you are, constraints, what matters)</small><textarea name="description" placeholder="Small specialty coffee roaster, €2M budget, wants to expand in 2027." rows={3} /></label>
    <button className="primary" type="submit">CREATE PROJECT</button>
  </form></Modal>
}

const HORIZONS = [['4h', '4 hours'], ['8h', '8 hours (session)'], ['1d', '1 day'], ['1w', '1 week'], ['1m', '1 month']] as const
const TIMEFRAMES = ['5m', '15m', '1h', '4h', '1d', '1wk', '1mo'] as const

export function TaskModal({ project, providers, defaults, onClose, onCreate }: { project: Project; providers: Provider[]; defaults: Settings | null; onClose: () => void; onCreate: (title: string, description: string, config: TaskConfig, files: File[]) => void }) {
  const [mode, setMode] = useState<'research' | 'trading'>('research')
  const [depth, setDepth] = useState<'quick' | 'standard' | 'deep'>((defaults?.depth as 'quick' | 'standard' | 'deep') ?? 'standard')
  const [style, setStyle] = useState<'daytrade' | 'swing'>('daytrade')
  const [horizon, setHorizon] = useState('8h')
  const [timeframes, setTimeframes] = useState<string[]>([])
  const [files, setFiles] = useState<File[]>([])
  const [symbols, setSymbols] = useState<MarketSymbol[]>([])
  const [dragging, setDragging] = useState(false)
  useEffect(() => { if (mode === 'trading' && symbols.length === 0) void api.marketSymbols().then(setSymbols).catch(() => undefined) }, [mode, symbols.length])
  const previews = useMemo(() => files.map((file) => ({ file, url: URL.createObjectURL(file) })), [files])
  useEffect(() => () => previews.forEach((preview) => URL.revokeObjectURL(preview.url)), [previews])

  const addFiles = (list: FileList | File[]) => setFiles((current) => [...current, ...Array.from(list).filter((file) => file.type.startsWith('image/'))].slice(0, 6))
  // Paste a chart screenshot straight from the clipboard.
  useEffect(() => {
    const onPaste = (event: ClipboardEvent) => { if (mode === 'trading' && event.clipboardData?.files.length) addFiles(event.clipboardData.files) }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [mode])

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const config: TaskConfig = { depth, mode }
    const provider = String(form.get('provider') ?? '')
    const model = String(form.get('model') ?? '').trim()
    if (provider) config.provider = provider
    if (model) config.model = model
    if (mode === 'trading') {
      const symbol = String(form.get('symbol') ?? '').trim()
      if (symbol) config.symbol = symbol
      else if (style === 'daytrade') config.symbol = 'XAUUSD'
      config.style = style
      config.horizon = horizon
      const replay = String(form.get('as_of') ?? '')
      if (replay) config.as_of = new Date(replay).toISOString()
      if (timeframes.length) config.timeframes = timeframes
      if (style === 'daytrade') {
        const number = (name: string) => Number(form.get(name))
        if (number('sl_pips') > 0) config.sl_pips = number('sl_pips')
        if (number('tp_pips') > 0) config.tp_pips = number('tp_pips')
        if (number('tp2_pips') > 0) config.tp2_pips = number('tp2_pips')
        if (number('pip') > 0) config.pip = number('pip')
        config.depth = depth
      }
    }
    onCreate(String(form.get('title') ?? ''), String(form.get('description') ?? ''), config, mode === 'trading' ? files : [])
  }
  return <Modal onClose={onClose}><form onSubmit={submit}>
    <p className="eyebrow">{project.name.toUpperCase()}</p><h2>{mode === 'trading' ? 'New trading desk task' : 'New research task'}</h2>
    <div className="segmented">{(['research', 'trading'] as const).map((value) => <button type="button" key={value} className={mode === value ? 'active' : ''} onClick={() => setMode(value)}>{value === 'research' ? 'Research' : 'Trading desk'}</button>)}</div>
    <label>{mode === 'trading' ? 'WHAT DO YOU WANT TO KNOW?' : 'WHAT DO YOU WANT TO FIND OUT?'}<input name="title" placeholder={mode === 'trading' ? (style === 'daytrade' ? 'Gold now: long, short or wait? Which line, which session?' : 'Where is USDJPY heading into tonight’s US CPI? Long or short?') : 'Should we launch our coffee brand in Japan next year?'} required autoFocus /></label>
    {mode === 'trading' && <>
      <div className="segmented">{(['daytrade', 'swing'] as const).map((value) => <button type="button" key={value} className={style === value ? 'active' : ''} onClick={() => { setStyle(value); setHorizon(value === 'daytrade' ? '8h' : '1d'); setDepth(value === 'daytrade' ? 'quick' : 'standard') }}>{value === 'daytrade' ? 'Day trade · pattern + sessions' : 'Swing · quant + fundamentals'}</button>)}</div>
      {style === 'daytrade' && <div className="field-row four">
        <label>STOP (pips)<input name="sl_pips" type="number" min={1} step={1} defaultValue={20} /></label>
        <label>TARGET 1 (pips)<input name="tp_pips" type="number" min={1} step={1} defaultValue={50} /></label>
        <label>TARGET 2 (pips)<input name="tp2_pips" type="number" min={1} step={1} defaultValue={100} /></label>
        <label>PIP SIZE <small>(gold 0.1)</small><input name="pip" type="number" min={0} step="any" placeholder="auto" /></label>
      </div>}
      <div className="field-row">
        <label>INSTRUMENT <small>(or let the desk detect it)</small><input name="symbol" list="market-symbols" placeholder={style === 'daytrade' ? 'XAUUSD (default)' : 'USDJPY, XAUUSD, EURUSD, SPY…'} />
          <datalist id="market-symbols">{symbols.map((symbol) => <option key={symbol.id} value={symbol.id}>{symbol.name}</option>)}</datalist></label>
        <label>HORIZON<select value={horizon} onChange={(event) => setHorizon(event.target.value)}>{HORIZONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      <label>REPLAY — TEST ON A PAST MOMENT <small>(optional, your local time: the desk sees nothing after it and the plan is scored at once)</small><input name="as_of" type="datetime-local" max={new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 16)} /></label>
      <div className="field-label">TIMEFRAMES <small>({style === 'daytrade' ? 'none = H4, H1, M15, M5' : 'none = desk chooses for the horizon'})</small></div>
      <div className="chip-select">{TIMEFRAMES.map((frame) => <button type="button" key={frame} className={timeframes.includes(frame) ? 'active' : ''} onClick={() => setTimeframes((current) => current.includes(frame) ? current.filter((item) => item !== frame) : [...current, frame])}>{frame}</button>)}</div>
      <div className="field-label">CHART SCREENSHOTS <small>(drop, paste or browse — best is H4 + H1 + M15 of the same pair; the desk reads top-down and takes a missing timeframe from market data)</small></div>
      <label className={`dropzone ${dragging ? 'dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files) }}>
        <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden onChange={(event) => { if (event.target.files) addFiles(event.target.files) }} />
        {previews.length === 0 ? <span>Drop chart images here, paste with ⌘V, or click to browse</span> : <div className="thumbs">{previews.map(({ file, url }, index) => <figure key={url}><img src={url} alt={file.name} /><button type="button" onClick={(event) => { event.preventDefault(); setFiles((current) => current.filter((_, item) => item !== index)) }} aria-label="Remove image"><X size={11} /></button></figure>)}</div>}
      </label>
    </>}
    <label>CONTEXT <small>(optional)</small><textarea name="description" placeholder={mode === 'trading' ? 'Your own prediction and lines (the desk tests it: what confirms it, what proves it wrong), your position, events you are worried about…' : 'Anything that helps: budget, timeline, what you already know, what a good answer looks like.'} rows={3} /></label>
    <div className="field-label">DEPTH</div>
    <div className="segmented">{(['quick', 'standard', 'deep'] as const).map((value) => <button type="button" key={value} className={depth === value ? 'active' : ''} onClick={() => setDepth(value)}>{value}</button>)}</div>
    <div className="field-row">
      <label>PROVIDER<select name="provider" defaultValue="">
        <option value="">Model router (settings)</option>
        {providers.map((provider) => <option key={provider.id} value={provider.id} disabled={!provider.available}>{provider.id}{provider.available ? '' : ' (unavailable)'}{provider.tested ? '' : ' · untested'}</option>)}
      </select></label>
      <label>MODEL <small>(optional)</small><input name="model" placeholder={defaults?.model ?? 'sonnet'} /></label>
    </div>
    <p className="orchestrator-copy">{mode === 'trading'
      ? (style === 'daytrade'
        ? 'Technical only: the desk reads how each session acted, the body-close lines on H4/H1/M15/M5 and the retest statistics, then gives long / short / wait with the line, trigger, fixed stop and targets, and the session to act in. Recorded and scored afterwards.'
        : 'The orchestrator loads live market data (prices, correlations, calendar, options positioning, ML probability), hires a trading desk, and returns a trade plan that is recorded and scored after its horizon. Probabilities, not guarantees.')
      : 'The orchestrator reads this, hires a team of specialist agents with matching skills, wires them into a dependency graph, and returns a decision with sources. Runs on your own CLI subscriptions or local models — no credits.'}</p>
    <button className="primary" type="submit">CREATE TASK</button>
  </form></Modal>
}

export function ReportModal({ title, report, onClose }: { title: string; report: string; onClose: () => void }) {
  return <Modal onClose={onClose} wide><p className="eyebrow">DECISION REPORT</p><h2>{title}</h2><Markdown text={report} className="report-full" /></Modal>
}

export function SettingsModal({ onClose, onSaved }: { onClose: () => void; onSaved: (settings: Settings) => void }) {
  const [tab, setTab] = useState<'run' | 'router' | 'skills' | 'appearance' | 'providers'>('run')
  const [settings, setSettings] = useState<Settings | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [embedModel, setEmbedModel] = useState('')
  const [saved, setSaved] = useState(false)
  const [error, setError] = useState<string | null>(null)
  useEffect(() => {
    void api.settings().then(setSettings).catch((caught) => setError(String(caught)))
    void api.providers().then((result) => { setProviders(result.providers); setEmbedModel(result.embed_model) }).catch(() => undefined)
  }, [])
  const update = <K extends keyof Settings>(key: K, value: Settings[K]) => { setSettings((current) => current ? { ...current, [key]: value } : current); setSaved(false) }
  async function save() {
    if (!settings) return
    try { const result = await api.saveSettings(settings); setSettings(result); setSaved(true); setError(null); onSaved(result) } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
  }
  const saveButton = (label: string) => <div className="settings-actions"><button className="primary" onClick={() => void save()}>{saved ? <Check size={14} /> : <Save size={14} />}{saved ? 'SAVED' : label}</button></div>
  const tabs = [['run', 'RUN DEFAULTS'], ['router', 'MODEL ROUTER'], ['skills', 'SKILLS'], ['appearance', 'APPEARANCE'], ['providers', 'PROVIDERS']] as const
  return <Modal onClose={onClose} wide>
    <p className="eyebrow">VIEW ENGINE</p><h2>Settings</h2>
    <div className="tabs">{tabs.map(([id, label]) => <button key={id} className={tab === id ? 'active' : ''} onClick={() => setTab(id)}>{label}</button>)}</div>
    {error && <div className="run-error">{error}</div>}
    <div className="settings-body">
      {tab === 'run' && settings && <div className="settings-grid">
        <div className="settings-section">TEAM</div>
        <label>DEPTH<select value={settings.depth} onChange={(event) => update('depth', event.target.value)}><option>quick</option><option>standard</option><option>deep</option></select></label>
        <label>MAX AGENTS<input type="number" min={3} max={24} value={settings.max_agents} onChange={(event) => update('max_agents', Number(event.target.value))} /></label>
        <label>AGENTS AT ONCE<input type="number" min={1} max={8} value={settings.concurrency} onChange={(event) => update('concurrency', Number(event.target.value))} /></label>
        <div className="settings-section">LOOPS AND LIMITS</div>
        <label>ITERATIONS PER AGENT<input type="number" min={1} max={5} value={settings.max_iterations} onChange={(event) => update('max_iterations', Number(event.target.value))} /></label>
        <label>CRITIC ROUNDS<input type="number" min={0} max={3} value={settings.max_rounds} onChange={(event) => update('max_rounds', Number(event.target.value))} /></label>
        <label>RETRY ATTEMPTS<input type="number" min={1} max={6} value={settings.max_attempts} onChange={(event) => update('max_attempts', Number(event.target.value))} /></label>
        <label>AGENT TIMEOUT (s)<input type="number" min={60} step={30} value={settings.agent_timeout_secs} onChange={(event) => update('agent_timeout_secs', Number(event.target.value))} /></label>
        <label>REASONING EFFORT<select value={settings.effort ?? ''} onChange={(event) => update('effort', event.target.value || null)}><option value="">CLI default (fast day trades use low)</option>{['low', 'medium', 'high', 'xhigh', 'max'].map((level) => <option key={level} value={level}>{level}</option>)}</select></label>
        <label className="check"><input type="checkbox" checked={settings.allow_agent_hiring} onChange={(event) => update('allow_agent_hiring', event.target.checked)} />Agents may hire specialists</label>
        <div className="settings-section">MEMORY</div>
        <p className="orchestrator-copy router-note">Long-term memory is always on: every run recalls what the project already knows, stores new findings and consolidates them. Embeddings: <b>{embedModel || '…'}</b>.</p>
        {saveButton('SAVE DEFAULTS')}
      </div>}
      {tab === 'router' && settings && <div className="settings-grid">
        <div className="settings-section">DEFAULT MODEL</div>
        <label>PROVIDER<select value={settings.provider} onChange={(event) => update('provider', event.target.value)}>{providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.id}{provider.available ? '' : ' (unavailable)'}</option>)}</select></label>
        <label>MODEL<input value={settings.model ?? ''} placeholder="sonnet" onChange={(event) => update('model', event.target.value || null)} /></label>
        <label>LIGHT MODEL <small>(intent, helpers)</small><input value={settings.utility_model ?? ''} placeholder="haiku" onChange={(event) => update('utility_model', event.target.value || null)} /></label>
        <div className="settings-section">ROUTE PER ROLE <small>— provider:model, empty = default</small></div>
        {ROUTE_ROLES.map((role) => <label key={role}>{role.toUpperCase()}<input value={settings.routes[role] ?? ''} placeholder="default" onChange={(event) => update('routes', { ...settings.routes, [role]: event.target.value })} /></label>)}
        <div className="settings-section">FALLBACK</div>
        <label className="wide-field">FALLBACK CHAIN <small>(comma-separated; used when a route hits its usage limit or is unavailable)</small><input value={settings.fallbacks.join(', ')} placeholder="claude:haiku, codex, ollama:qwen3" onChange={(event) => update('fallbacks', event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} /></label>
        <p className="orchestrator-copy router-note">CLI providers (Claude Code, Codex, Copilot) run on your own subscription limits; Ollama and OpenAI-compatible servers run locally. Examples: claude:sonnet · claude:opus · codex · ollama:qwen3 · simulated.</p>
        {saveButton('SAVE ROUTES')}
      </div>}
      {tab === 'skills' && <SkillLibrary />}
      {tab === 'appearance' && <AppearancePanel />}
      {tab === 'providers' && <div className="provider-list">
        {providers.map((provider) => <div key={provider.id} className={`provider-row ${provider.available ? 'ok' : ''}`}>
          <b>{provider.id}</b>
          <span>{provider.available ? 'available' : 'unavailable'}{provider.tested ? '' : ' · adapter untested'}{provider.native_web ? ' · native web' : ' · harness web search'}</span>
          <small>{provider.detail}{provider.model ? ` · model ${provider.model}` : ''}</small>
        </div>)}
        <p className="orchestrator-copy">Configure binaries and endpoints with env vars on the backend: CLAUDE_BIN, CLAUDE_MODEL, CODEX_BIN, CODEX_MODEL, COPILOT_BIN, OLLAMA_URL, OLLAMA_MODEL, OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_API_KEY, SEARXNG_URL, OANDA_API_TOKEN (quant sidecar).</p>
      </div>}
    </div>
  </Modal>
}

function AppearancePanel() {
  const [value, setValue] = useState<Appearance>(loadAppearance)
  const change = <K extends keyof Appearance>(key: K, next: Appearance[K]) => setValue((current) => { const updated = { ...current, [key]: next }; applyAppearance(updated); return updated })
  return <div className="settings-grid">
    <div className="settings-section">TYPE</div>
    <label>TEXT FONT<select value={value.sans} onChange={(event) => change('sans', event.target.value)}>{SANS_FONTS.map((font) => <option key={font}>{font}</option>)}</select></label>
    <label>DATA FONT<select value={value.mono} onChange={(event) => change('mono', event.target.value)}>{MONO_FONTS.map((font) => <option key={font}>{font}</option>)}</select></label>
    <label>TEXT SIZE · {Math.round(value.scale * 100)}%<input type="range" min={0.85} max={1.4} step={0.05} value={value.scale} onChange={(event) => change('scale', Number(event.target.value))} /></label>
    <div className="settings-section">COLOUR</div>
    <label className="wide-field">ACCENT<div className="swatches">{ACCENTS.map((color) => <button type="button" key={color} className={value.accent === color ? 'active' : ''} style={{ background: color }} onClick={() => change('accent', color)} aria-label={`Accent ${color}`} />)}</div></label>
    <div className="settings-section">LAYOUT</div>
    <label>HEADER HEIGHT · {value.topbar}px<input type="range" min={44} max={80} step={2} value={value.topbar} onChange={(event) => change('topbar', Number(event.target.value))} /></label>
    <label>LEFT PANEL · {value.left}px<input type="range" min={220} max={420} step={10} value={value.left} onChange={(event) => change('left', Number(event.target.value))} /></label>
    <label>RIGHT PANEL · {value.right}px<input type="range" min={280} max={520} step={10} value={value.right} onChange={(event) => change('right', Number(event.target.value))} /></label>
    <div className="settings-actions"><button className="secondary" onClick={() => { setValue(DEFAULT_APPEARANCE); applyAppearance(DEFAULT_APPEARANCE) }}>RESET TO DEFAULT</button></div>
    <p className="orchestrator-copy router-note">Changes apply immediately and are remembered in this browser.</p>
  </div>
}

const SKILL_TEMPLATE = `---
name: my-skill
description: Use when … (one sentence — the skill index and the orchestrator match against this)
domain: research
tags: [keyword, keyword]
---
# Title

## Role charter

## Core knowledge

## Research method

## Analysis checklist

## Output contract

## Pitfalls
`

function SkillLibrary() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [filter, setFilter] = useState('')
  const [open, setOpen] = useState<Skill | null>(null)
  const [editing, setEditing] = useState<{ name: string; markdown: string } | null>(null)
  const [source, setSource] = useState('')
  const [importing, setImporting] = useState(false)
  const [result, setResult] = useState<SkillImport | null>(null)
  const [error, setError] = useState<string | null>(null)
  const fail = (caught: unknown) => setError(caught instanceof Error ? caught.message : String(caught))
  // Typing a query asks the engine's skill index, the same ranking the orchestrator uses when hiring.
  useEffect(() => {
    const timer = window.setTimeout(() => void api.skills(filter.trim() || undefined).then(setSkills).catch(fail), filter ? 200 : 0)
    return () => window.clearTimeout(timer)
  }, [filter])
  const reload = () => void api.skills(filter.trim() || undefined).then(setSkills).catch(fail)
  const domains = useMemo(() => filter.trim() ? ['best matches'] : [...new Set(skills.map((skill) => skill.domain))].sort(), [skills, filter])

  async function save() {
    if (!editing) return
    try { await api.saveSkill(editing.name, editing.markdown); setEditing(null); setError(null); reload() } catch (caught) { fail(caught) }
  }
  async function runImport() {
    if (!source.trim()) return
    setImporting(true); setError(null)
    try { setResult(await api.importSkills(source.trim(), false)); reload() } catch (caught) { fail(caught) } finally { setImporting(false) }
  }
  async function remove(skill: Skill) {
    if (!window.confirm(`Delete skill “${skill.name}” from your library?`)) return
    try { await api.deleteSkill(skill.name); setOpen(null); reload() } catch (caught) { fail(caught) }
  }

  if (editing) return <div className="skill-editor">
    <label>SKILL NAME<input value={editing.name} onChange={(event) => setEditing({ ...editing, name: event.target.value })} placeholder="my-skill" /></label>
    <label>SKILL.md<textarea value={editing.markdown} onChange={(event) => setEditing({ ...editing, markdown: event.target.value })} rows={18} spellCheck={false} /></label>
    {error && <div className="run-error">{error}</div>}
    <div className="settings-actions"><button className="secondary" onClick={() => setEditing(null)}>CANCEL</button><button className="primary" onClick={() => void save()}><Save size={14} />SAVE SKILL</button></div>
  </div>
  if (open) return <div className="skill-view">
    <div className="skill-view-head"><button className="secondary" onClick={() => setOpen(null)}>← ALL SKILLS</button><span>
      <button className="secondary" onClick={() => setEditing({ name: open.name, markdown: `---\nname: ${open.name}\ndescription: ${open.description}\ndomain: ${open.domain}\ntags: [${open.tags.join(', ')}]\n---\n${open.body ?? ''}` })}>EDIT</button>{' '}
      <button className="secondary" onClick={() => void remove(open)}>DELETE</button></span></div>
    <p className="orchestrator-copy">{open.description}<br /><small>source: {open.source || 'local'} · ~{open.tokens} tokens</small></p>
    <Markdown text={open.body ?? ''} className="report-inline" />
  </div>
  return <div className="skill-library">
    <div className="skill-toolbar">
      <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={`Search ${skills.length} skills the way the engine matches them…`} aria-label="Search skills" />
      <button className="secondary" onClick={() => void api.skills().then(setSkills)} title="Reload"><RefreshCw size={12} /></button>
      <button className="primary" onClick={() => setEditing({ name: '', markdown: SKILL_TEMPLATE })}>NEW SKILL</button>
    </div>
    <div className="import-row">
      <input value={source} onChange={(event) => setSource(event.target.value)} placeholder="Import from GitHub: owner/repo, owner/repo/path or a github.com URL (e.g. anthropics/skills)" aria-label="GitHub source" />
      <button className="secondary" disabled={importing} onClick={() => void runImport()}>{importing ? 'IMPORTING…' : 'IMPORT'}</button>
    </div>
    {result && <div className="import-result">{result.source}: found {result.found} · imported {result.imported.length}{result.skipped_existing.length ? ` · skipped ${result.skipped_existing.length} already in library` : ''}{result.failed.length ? ` · ${result.failed.length} failed (${result.failed[0].reason})` : ''}</div>}
    {error && <div className="run-error">{error}</div>}
    {skills.length === 0 && !filter && <div className="empty">Your skill library is empty. Write a skill, import a pack from GitHub, or drop folders into the <b>skills/</b> directory. Agents work without skills too — skills make them experts.</div>}
    {domains.map((domain) => <section key={domain}><div className="field-label">{domain.toUpperCase()}</div>
      <div className="skill-grid">{skills.filter((skill) => filter.trim() || skill.domain === domain).map((skill) => <button key={skill.name} className="skill-card" onClick={() => void api.skill(skill.name).then(setOpen)}>
        <b>{skill.name}</b><small>{skill.description}</small>
        <em>~{skill.tokens} tokens{skill.source && skill.source !== 'local' ? <span className="skill-source"> · {skill.source.replace('github:', '')}</span> : null}</em>
        {skill.stats && skill.stats.uses > 0 && <em className="skill-stats">used {skill.stats.uses}×{skill.stats.accepted + skill.stats.revised > 0 ? ` · accepted ${skill.stats.accepted}/${skill.stats.accepted + skill.stats.revised}` : ''}{skill.stats.hits + skill.stats.misses > 0 ? ` · calls hit ${skill.stats.hits}/${skill.stats.hits + skill.stats.misses}` : ''}</em>}
      </button>)}</div>
    </section>)}
  </div>
}
