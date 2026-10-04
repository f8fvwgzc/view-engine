import { FormEvent, ReactNode, useEffect, useMemo, useState } from 'react'
import { Check, RefreshCw, Save, X } from 'lucide-react'
import { api } from '../api'
import { ROUTE_ROLES, type MarketSymbol, type Project, type Provider, type Settings, type Skill, type TaskConfig } from '../types'
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

const HORIZONS = [['4h', '4 hours'], ['1d', '1 day'], ['1w', '1 week'], ['1m', '1 month']] as const
const TIMEFRAMES = ['15m', '1h', '4h', '1d', '1wk', '1mo'] as const

export function TaskModal({ project, providers, defaults, onClose, onCreate }: { project: Project; providers: Provider[]; defaults: Settings | null; onClose: () => void; onCreate: (title: string, description: string, config: TaskConfig, files: File[]) => void }) {
  const [mode, setMode] = useState<'research' | 'trading'>('research')
  const [depth, setDepth] = useState<'quick' | 'standard' | 'deep'>((defaults?.depth as 'quick' | 'standard' | 'deep') ?? 'standard')
  const [horizon, setHorizon] = useState('1d')
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
      config.horizon = horizon
      if (timeframes.length) config.timeframes = timeframes
    }
    onCreate(String(form.get('title') ?? ''), String(form.get('description') ?? ''), config, mode === 'trading' ? files : [])
  }
  return <Modal onClose={onClose}><form onSubmit={submit}>
    <p className="eyebrow">{project.name.toUpperCase()}</p><h2>{mode === 'trading' ? 'New trading desk task' : 'New research task'}</h2>
    <div className="segmented">{(['research', 'trading'] as const).map((value) => <button type="button" key={value} className={mode === value ? 'active' : ''} onClick={() => setMode(value)}>{value === 'research' ? 'Research' : 'Trading desk'}</button>)}</div>
    <label>{mode === 'trading' ? 'WHAT DO YOU WANT TO KNOW?' : 'WHAT DO YOU WANT TO FIND OUT?'}<input name="title" placeholder={mode === 'trading' ? 'Where is USDJPY heading into tonight’s US CPI? Long or short?' : 'Should we launch our coffee brand in Japan next year?'} required autoFocus /></label>
    {mode === 'trading' && <>
      <div className="field-row">
        <label>INSTRUMENT <small>(or let the desk detect it)</small><input name="symbol" list="market-symbols" placeholder="USDJPY, XAUUSD, EURUSD, SPY…" />
          <datalist id="market-symbols">{symbols.map((symbol) => <option key={symbol.id} value={symbol.id}>{symbol.name}</option>)}</datalist></label>
        <label>HORIZON<select value={horizon} onChange={(event) => setHorizon(event.target.value)}>{HORIZONS.map(([value, label]) => <option key={value} value={value}>{label}</option>)}</select></label>
      </div>
      <div className="field-label">TIMEFRAMES <small>(none = desk chooses for the horizon)</small></div>
      <div className="chip-select">{TIMEFRAMES.map((frame) => <button type="button" key={frame} className={timeframes.includes(frame) ? 'active' : ''} onClick={() => setTimeframes((current) => current.includes(frame) ? current.filter((item) => item !== frame) : [...current, frame])}>{frame}</button>)}</div>
      <div className="field-label">CHART SCREENSHOTS <small>(drop, paste or browse — the chart reader agent studies them)</small></div>
      <label className={`dropzone ${dragging ? 'dragging' : ''}`} onDragOver={(event) => { event.preventDefault(); setDragging(true) }} onDragLeave={() => setDragging(false)} onDrop={(event) => { event.preventDefault(); setDragging(false); addFiles(event.dataTransfer.files) }}>
        <input type="file" accept="image/png,image/jpeg,image/webp,image/gif" multiple hidden onChange={(event) => { if (event.target.files) addFiles(event.target.files) }} />
        {previews.length === 0 ? <span>Drop chart images here, paste with ⌘V, or click to browse</span> : <div className="thumbs">{previews.map(({ file, url }, index) => <figure key={url}><img src={url} alt={file.name} /><button type="button" onClick={(event) => { event.preventDefault(); setFiles((current) => current.filter((_, item) => item !== index)) }} aria-label="Remove image"><X size={11} /></button></figure>)}</div>}
      </label>
    </>}
    <label>CONTEXT <small>(optional)</small><textarea name="description" placeholder={mode === 'trading' ? 'Your position, risk tolerance, levels you care about, events you are worried about…' : 'Anything that helps: budget, timeline, what you already know, what a good answer looks like.'} rows={3} /></label>
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
      ? 'The orchestrator loads live market data (prices, correlations, calendar, options positioning, ML probability), hires a trading desk, and returns a trade plan that is recorded and scored after its horizon. Probabilities, not guarantees.'
      : 'The orchestrator reads this, hires a team of specialist agents with matching skills, wires them into a dependency graph, and returns a decision with sources. Runs on your own CLI subscriptions or local models — no credits.'}</p>
    <button className="primary" type="submit">CREATE TASK</button>
  </form></Modal>
}

export function ReportModal({ title, report, onClose }: { title: string; report: string; onClose: () => void }) {
  return <Modal onClose={onClose} wide><p className="eyebrow">DECISION REPORT</p><h2>{title}</h2><Markdown text={report} className="report-full" /></Modal>
}

export function SettingsModal({ onClose, onSaved }: { onClose: () => void; onSaved: (settings: Settings) => void }) {
  const [tab, setTab] = useState<'run' | 'router' | 'providers' | 'skills'>('run')
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
    try { const result = await api.saveSettings(settings); setSettings(result); setSaved(true); onSaved(result) } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
  }
  return <Modal onClose={onClose} wide>
    <p className="eyebrow">ENGINE</p><h2>Settings</h2>
    <div className="tabs"><button className={tab === 'run' ? 'active' : ''} onClick={() => setTab('run')}>RUN DEFAULTS</button><button className={tab === 'router' ? 'active' : ''} onClick={() => setTab('router')}>MODEL ROUTER</button><button className={tab === 'providers' ? 'active' : ''} onClick={() => setTab('providers')}>PROVIDERS</button><button className={tab === 'skills' ? 'active' : ''} onClick={() => setTab('skills')}>SKILLS</button></div>
    {error && <div className="run-error">{error}</div>}
    {tab === 'run' && settings && <div className="settings-grid">
      <label>PROVIDER<select value={settings.provider} onChange={(event) => update('provider', event.target.value)}>{providers.map((provider) => <option key={provider.id} value={provider.id}>{provider.id}{provider.available ? '' : ' (unavailable)'}</option>)}</select></label>
      <label>MODEL<input value={settings.model ?? ''} placeholder="sonnet" onChange={(event) => update('model', event.target.value || null)} /></label>
      <label>UTILITY MODEL <small>(planning helpers)</small><input value={settings.utility_model ?? ''} placeholder="haiku" onChange={(event) => update('utility_model', event.target.value || null)} /></label>
      <label>DEPTH<select value={settings.depth} onChange={(event) => update('depth', event.target.value)}><option>quick</option><option>standard</option><option>deep</option></select></label>
      <label>MAX AGENTS<input type="number" min={3} max={24} value={settings.max_agents} onChange={(event) => update('max_agents', Number(event.target.value))} /></label>
      <label>CONCURRENCY<input type="number" min={1} max={8} value={settings.concurrency} onChange={(event) => update('concurrency', Number(event.target.value))} /></label>
      <label>ITERATIONS PER AGENT<input type="number" min={1} max={5} value={settings.max_iterations} onChange={(event) => update('max_iterations', Number(event.target.value))} /></label>
      <label>CRITIC ROUNDS<input type="number" min={0} max={3} value={settings.max_rounds} onChange={(event) => update('max_rounds', Number(event.target.value))} /></label>
      <label>RETRY ATTEMPTS<input type="number" min={1} max={6} value={settings.max_attempts} onChange={(event) => update('max_attempts', Number(event.target.value))} /></label>
      <label>AGENT TIMEOUT (s)<input type="number" min={60} step={30} value={settings.agent_timeout_secs} onChange={(event) => update('agent_timeout_secs', Number(event.target.value))} /></label>
      <label className="check"><input type="checkbox" checked={settings.memory} onChange={(event) => update('memory', event.target.checked)} />Long-term memory</label>
      <label className="check"><input type="checkbox" checked={settings.allow_agent_hiring} onChange={(event) => update('allow_agent_hiring', event.target.checked)} />Agents may hire specialists</label>
      <div className="settings-actions"><button className="primary" onClick={() => void save()}>{saved ? <Check size={14} /> : <Save size={14} />}{saved ? 'SAVED' : 'SAVE DEFAULTS'}</button></div>
    </div>}
    {tab === 'router' && settings && <div className="settings-grid">
      <p className="orchestrator-copy router-note">Each agent role runs on its own <b>provider:model</b>. CLI providers (Claude Code, Codex, Copilot) use your own subscription limits; Ollama/OpenAI-compatible run locally. Leave a role empty to use the default ({settings.provider}{settings.model ? `:${settings.model}` : ''}; intent/utility use {settings.utility_model ?? settings.model ?? 'default'}). Examples: claude:sonnet · claude:haiku · claude:opus · codex · ollama:qwen3 · simulated.</p>
      {ROUTE_ROLES.map((role) => <label key={role}>{role.toUpperCase()}<input value={settings.routes[role] ?? ''} placeholder="default" onChange={(event) => update('routes', { ...settings.routes, [role]: event.target.value })} /></label>)}
      <label className="wide-field">FALLBACK CHAIN <small>(comma-separated, used when a route hits its usage limit or is unavailable)</small><input value={settings.fallbacks.join(', ')} placeholder="claude:haiku, codex, ollama:qwen3" onChange={(event) => update('fallbacks', event.target.value.split(',').map((item) => item.trim()).filter(Boolean))} /></label>
      <div className="settings-actions"><button className="primary" onClick={() => void save()}>{saved ? <Check size={14} /> : <Save size={14} />}{saved ? 'SAVED' : 'SAVE ROUTES'}</button></div>
    </div>}
    {tab === 'providers' && <div className="provider-list">
      {providers.map((provider) => <div key={provider.id} className={`provider-row ${provider.available ? 'ok' : ''}`}>
        <b>{provider.id}</b>
        <span>{provider.available ? 'available' : 'unavailable'}{provider.tested ? '' : ' · adapter untested'}{provider.native_web ? ' · native web' : ' · harness web search'}</span>
        <small>{provider.detail}{provider.model ? ` · model ${provider.model}` : ''}</small>
      </div>)}
      <p className="orchestrator-copy">Memory embeddings: <b>{embedModel}</b>. Configure binaries and endpoints with env vars on the backend: CLAUDE_BIN, CLAUDE_MODEL, CODEX_BIN, CODEX_MODEL, COPILOT_BIN, OLLAMA_URL, OLLAMA_MODEL, OPENAI_BASE_URL, OPENAI_MODEL, OPENAI_API_KEY, SEARXNG_URL.</p>
    </div>}
    {tab === 'skills' && <SkillLibrary />}
  </Modal>
}

const SKILL_TEMPLATE = `---
name: my-skill
description: Use when … (one sentence the orchestrator uses to decide whether to attach this skill)
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
  const [error, setError] = useState<string | null>(null)
  const load = () => void api.skills().then(setSkills).catch((caught) => setError(String(caught)))
  useEffect(load, [])
  const visible = useMemo(() => {
    const needle = filter.toLowerCase()
    return skills.filter((skill) => !needle || `${skill.name} ${skill.domain} ${skill.description} ${skill.tags.join(' ')}`.toLowerCase().includes(needle))
  }, [skills, filter])
  const domains = useMemo(() => [...new Set(visible.map((skill) => skill.domain))].sort(), [visible])

  async function save() {
    if (!editing) return
    try { await api.saveSkill(editing.name, editing.markdown); setEditing(null); load() } catch (caught) { setError(caught instanceof Error ? caught.message : String(caught)) }
  }

  if (editing) return <div className="skill-editor">
    <label>SKILL NAME<input value={editing.name} onChange={(event) => setEditing({ ...editing, name: event.target.value })} placeholder="my-skill" /></label>
    <label>SKILL.md<textarea value={editing.markdown} onChange={(event) => setEditing({ ...editing, markdown: event.target.value })} rows={18} spellCheck={false} /></label>
    {error && <div className="run-error">{error}</div>}
    <div className="settings-actions"><button className="secondary" onClick={() => setEditing(null)}>CANCEL</button><button className="primary" onClick={() => void save()}><Save size={14} />SAVE SKILL</button></div>
  </div>
  if (open) return <div className="skill-view">
    <div className="skill-view-head"><button className="secondary" onClick={() => setOpen(null)}>← ALL SKILLS</button><button className="secondary" onClick={() => setEditing({ name: open.name, markdown: `---\nname: ${open.name}\ndescription: ${open.description}\ndomain: ${open.domain}\ntags: [${open.tags.join(', ')}]\n---\n${open.body ?? ''}` })}>EDIT</button></div>
    <p className="orchestrator-copy">{open.description}</p>
    <Markdown text={open.body ?? ''} className="report-inline" />
  </div>
  return <div className="skill-library">
    <div className="skill-toolbar">
      <input value={filter} onChange={(event) => setFilter(event.target.value)} placeholder={`Filter ${skills.length} skills…`} aria-label="Filter skills" />
      <button className="secondary" onClick={load} title="Reload"><RefreshCw size={12} /></button>
      <button className="primary" onClick={() => setEditing({ name: '', markdown: SKILL_TEMPLATE })}>NEW SKILL</button>
    </div>
    {error && <div className="run-error">{error}</div>}
    {domains.map((domain) => <section key={domain}><div className="field-label">{domain.toUpperCase()}</div>
      <div className="skill-grid">{visible.filter((skill) => skill.domain === domain).map((skill) => <button key={skill.name} className="skill-card" onClick={() => void api.skill(skill.name).then(setOpen)}>
        <b>{skill.name}</b><small>{skill.description}</small><em>~{skill.tokens} tokens</em>
      </button>)}</div>
    </section>)}
  </div>
}
