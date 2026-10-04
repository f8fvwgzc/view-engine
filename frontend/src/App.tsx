import { lazy, Suspense, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { BellRing, Brain, ChevronRight, Crosshair, CircleDot, FolderPlus, Radio, Settings as SettingsIcon, Trash2, X } from 'lucide-react'
import { api } from './api'
import { MemoryPanel } from './components/MemoryPanel'
import { TaskPanel } from './components/TaskPanel'
import { PredictionsPanel } from './components/Trading'
import { timeLabel, type PredictionBook, type Project, type Provider, type RunDetail, type Settings, type SwarmEvent, type TaskConfig } from './types'
import { useLiveEvents } from './useLiveEvents'
import { useUrlSelection } from './useUrlSelection'

// Code-split the heavy parts: the graph (React Flow) and the modals load on first use.
const AgentGraph = lazy(() => import('./AgentGraph').then((module) => ({ default: module.AgentGraph })))
const ProjectModal = lazy(() => import('./components/Modals').then((module) => ({ default: module.ProjectModal })))
const TaskModal = lazy(() => import('./components/Modals').then((module) => ({ default: module.TaskModal })))
const SettingsModal = lazy(() => import('./components/Modals').then((module) => ({ default: module.SettingsModal })))
const ReportModal = lazy(() => import('./components/Modals').then((module) => ({ default: module.ReportModal })))

const MAX_EVENTS = 300
const MAX_NOTIFICATIONS = 2
// Toasts only for run milestones of tasks you are not looking at; the selected run already shows in the graph.
const NOTIFY_KINDS = new Set(['run_complete', 'run_failed', 'round_started'])

type Notification = Pick<SwarmEvent, 'id' | 'kind' | 'message'>

export function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [events, setEvents] = useState<SwarmEvent[]>([])
  const [skillCount, setSkillCount] = useState(0)
  const { project: selectedProjectId, task: selectedTaskId, agent: selectedAgent, selectProject, selectTask: setSelectedTaskId, selectAgent: setSelectedAgent } = useUrlSelection()
  const [detail, setDetail] = useState<RunDetail | null>(null)
  const [leftTab, setLeftTab] = useState<'stream' | 'memory' | 'predictions'>('stream')
  const [book, setBook] = useState<PredictionBook | null>(null)
  const [memoryKey, setMemoryKey] = useState(0)
  const [bookKey, setBookKey] = useState(0)
  const [modal, setModal] = useState<'project' | 'task' | 'settings' | 'report' | null>(null)
  const [providers, setProviders] = useState<Provider[]>([])
  const [limits, setLimits] = useState<Record<string, { status?: string; resetsAt?: number; rateLimitType?: string }>>({})
  const [defaults, setDefaults] = useState<Settings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notifications, setNotifications] = useState<Notification[]>([])
  const dashboardTimer = useRef<number | undefined>(undefined)
  const selectedProjectRef = useRef(selectedProjectId)
  selectedProjectRef.current = selectedProjectId
  const runTimer = useRef<number | undefined>(undefined)

  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null
  const selectedTask = selectedProject?.tasks.find((task) => task.id === selectedTaskId) ?? null
  const runId = selectedTask?.latest_run_id ?? null
  const projectEvents = useMemo(() => events.filter((event) => !selectedProjectId || event.project_id === selectedProjectId), [events, selectedProjectId])
  const agent = detail?.agents.find((item) => item.key === selectedAgent) ?? null
  const working = detail?.agents.filter((item) => item.status === 'running').length ?? 0

  const fail = (caught: unknown) => setError(caught instanceof Error ? caught.message : String(caught))

  const loadDashboard = useCallback(async () => {
    try {
      const dashboard = await api.dashboard()
      setProjects(dashboard.projects)
      setEvents((current) => {
        const seen = new Set(dashboard.events.map((event) => event.id))
        return [...current.filter((event) => !seen.has(event.id)), ...dashboard.events].sort((a, b) => b.created_at.localeCompare(a.created_at)).slice(0, MAX_EVENTS)
      })
      setSkillCount(dashboard.skills)
      if (!dashboard.projects.some((project) => project.id === selectedProjectRef.current)) selectProject(dashboard.projects[0]?.id ?? null)
      setError(null)
    } catch (caught) { fail(caught) }
  }, [selectProject])

  const loadRun = useCallback(async (id: string | null) => {
    if (!id) { setDetail(null); return }
    try { setDetail(await api.run(id)) } catch (caught) { fail(caught) }
  }, [])

  const scheduleDashboard = useCallback(() => {
    window.clearTimeout(dashboardTimer.current)
    dashboardTimer.current = window.setTimeout(() => void loadDashboard(), 600)
  }, [loadDashboard])

  const connected = useLiveEvents((event) => {
    setEvents((current) => current.some((item) => item.id === event.id) ? current : [event, ...current].slice(0, MAX_EVENTS))
    if (event.run_id && event.run_id === runId) {
      window.clearTimeout(runTimer.current)
      runTimer.current = window.setTimeout(() => void loadRun(event.run_id), 350)
    }
    if (event.kind === 'memory_retained' || event.kind === 'memory_consolidated') setMemoryKey((value) => value + 1)
    if (event.kind === 'prediction_recorded' || event.kind === 'prediction_scored') setBookKey((value) => value + 1)
    // Market signals always notify: they are about timing, the whole point is to see them immediately.
    if ((NOTIFY_KINDS.has(event.kind) && event.task_id !== selectedTaskId) || event.kind === 'market_signal') {
      setNotifications((current) => [{ id: event.id, kind: event.kind, message: event.message }, ...current].slice(0, MAX_NOTIFICATIONS))
      window.setTimeout(() => setNotifications((current) => current.filter((item) => item.id !== event.id)), 7000)
    }
    if (['run_started', 'run_complete', 'run_failed', 'agent_hired', 'agent_result'].includes(event.kind)) scheduleDashboard()
  }, () => { void loadDashboard(); void loadRun(runId) })

  useEffect(() => { void loadDashboard() }, [loadDashboard])
  useEffect(() => {
    if (!selectedProjectId) { setBook(null); return }
    void api.predictions(selectedProjectId).then(setBook).catch(() => setBook(null))
  }, [selectedProjectId, bookKey])
  useEffect(() => { void loadRun(runId) }, [runId, loadRun])
  // A new run of the same task clears agent focus; the first load (e.g. from a shared URL) keeps it.
  const previousRun = useRef(runId)
  useEffect(() => {
    if (previousRun.current && runId && previousRun.current !== runId) setSelectedAgent(null)
    previousRun.current = runId
  }, [runId, setSelectedAgent])
  useEffect(() => {
    void api.settings().then(setDefaults).catch(() => undefined)
    // Subscription window status as last reported by the CLI (no money involved: limits are your plan's own).
    const refresh = () => void api.providers().then((result) => { setProviders(result.providers); setLimits(result.limits ?? {}) }).catch(() => undefined)
    refresh()
    const timer = window.setInterval(refresh, 30_000)
    return () => window.clearInterval(timer)
  }, [])
  useEffect(() => () => { window.clearTimeout(dashboardTimer.current); window.clearTimeout(runTimer.current) }, [])

  async function createProject(name: string, description: string) {
    try {
      const project = await api.createProject(name, description)
      setProjects((current) => [{ ...project, tasks: [] }, ...current])
      selectProject(project.id)
      setModal(null)
    } catch (caught) { fail(caught) }
  }

  async function deleteProject(id: string) {
    try { await api.deleteProject(id); if (id === selectedProjectId) selectProject(null); await loadDashboard() } catch (caught) { fail(caught) }
  }

  async function createTask(title: string, description: string, config: TaskConfig, files: File[] = []) {
    if (!selectedProjectId) return
    try {
      const task = await api.createTask(selectedProjectId, title, description, config)
      if (files.length) await api.uploadAttachments(task.id, files)
      setProjects((current) => current.map((project) => project.id === selectedProjectId ? { ...project, tasks: [task, ...project.tasks] } : project))
      setSelectedTaskId(task.id)
      setModal(null)
    } catch (caught) { fail(caught) }
  }

  async function deleteTask(id: string) {
    try { await api.deleteTask(id); if (id === selectedTaskId) setSelectedTaskId(null); await loadDashboard() } catch (caught) { fail(caught) }
  }

  async function runTask(id: string) {
    try {
      const run = await api.runTask(id)
      setProjects((current) => current.map((project) => ({ ...project, tasks: project.tasks.map((task) => task.id === id ? { ...task, status: 'running', latest_run_id: run.id } : task) })))
      await loadRun(run.id)
    } catch (caught) { fail(caught) }
  }

  async function cancelRun(id: string) {
    try { await api.cancelRun(id) } catch (caught) { fail(caught) }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">◉</span> VIEW <span>ENGINE</span></div>
        <div className="system-readout">
          <span>MODE <b>RESEARCH</b></span><i />
          <span>WORKING <b>{working}</b></span><i />
          <span>SKILLS <b>{skillCount}</b></span><i />
          <span>NET <b className={connected ? 'ok' : 'bad'}>{connected ? 'SYNCED' : 'RECONNECTING'}</b></span>
        </div>
        {Object.entries(limits).map(([provider, limit]) => <span key={provider} className={`limit-pill ${limit.status === 'allowed' ? 'ok' : 'bad'}`} title="Your own subscription window as reported by the CLI">
          {provider.toUpperCase()} {limit.status === 'allowed' ? 'OK' : 'LIMIT'}{limit.rateLimitType ? ` · ${limit.rateLimitType.replace('_', ' ')}` : ''}{limit.resetsAt ? ` · resets ${new Date(limit.resetsAt * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false })}` : ''}
        </span>)}
        <button className="icon-button" onClick={() => setModal('settings')} aria-label="Settings" title="Settings, providers and skills"><SettingsIcon size={15} /></button>
        <LiveClock />
      </header>

      {error && <div className="error-banner" role="alert"><span>{error}</span><button onClick={() => setError(null)} aria-label="Dismiss error"><X size={13} /></button></div>}

      <aside className="left-panel">
        <section className="panel-section">
          <div className="section-label"><CircleDot size={13} /> PROJECTS</div>
          <button className="icon-button" onClick={() => setModal('project')} aria-label="Create project"><FolderPlus size={16} /></button>
        </section>
        <div className="project-list">
          {projects.length === 0 && <div className="empty">Create a project, then ask it research questions.</div>}
          {projects.map((project) => <div key={project.id} role="button" tabIndex={0} className={`project-row ${project.id === selectedProjectId ? 'selected' : ''}`} onClick={() => selectProject(project.id)} onKeyDown={(event) => { if (event.key === 'Enter') selectProject(project.id) }}>
            <span><b>{project.name}</b><small>{project.tasks.length} task{project.tasks.length === 1 ? '' : 's'} · {project.memory_count} memories</small></span>
            <button className="ghost-icon" onClick={(event) => { event.stopPropagation(); if (window.confirm(`Delete project “${project.name}”, its tasks, runs and memory?`)) void deleteProject(project.id) }} aria-label="Delete project"><Trash2 size={12} /></button>
            <ChevronRight size={14} />
          </div>)}
        </div>
        <div className="left-tabs">
          <button className={leftTab === 'stream' ? 'active' : ''} onClick={() => setLeftTab('stream')}><Radio size={12} /> STREAM</button>
          <button className={leftTab === 'memory' ? 'active' : ''} onClick={() => setLeftTab('memory')} disabled={!selectedProjectId}><Brain size={12} /> MEMORY</button>
          <button className={leftTab === 'predictions' ? 'active' : ''} onClick={() => setLeftTab('predictions')} disabled={!selectedProjectId}><Crosshair size={12} /> CALLS</button>
          <span className={`live-dot ${connected ? '' : 'off'}`}>{connected ? 'LIVE' : 'OFFLINE'}</span>
        </div>
        {leftTab === 'stream' || !selectedProjectId
          ? <div className="event-stream">
            {projectEvents.length === 0 && <div className="empty">Agent telemetry appears here during a run.</div>}
            {projectEvents.slice(0, 80).map((event) => <EventRow key={event.id} event={event} onSelectAgent={(key) => { if (event.task_id) setSelectedTaskId(event.task_id); setSelectedAgent(key) }} />)}
          </div>
          : leftTab === 'memory' ? <MemoryPanel projectId={selectedProjectId} refreshKey={memoryKey} /> : <PredictionsPanel book={book} projectId={selectedProjectId} />}
      </aside>

      <section className="workspace">
        <div className="workspace-toolbar">
          <div className="workspace-title"><p className="eyebrow">{selectedProject?.name ?? 'NO PROJECT'}</p><h1>{selectedTask?.title ?? 'Select or create a research task'}</h1></div>
        </div>
        <div className="workspace-body">
          <Suspense fallback={<div className="agent-graph"><div className="canvas-empty">Loading graph…</div></div>}><AgentGraph detail={selectedTask ? detail : null} liveEvents={events} selectedAgent={selectedAgent} onSelectAgent={setSelectedAgent}
            emptyHint={selectedTask ? 'Start the research to watch the orchestrator hire its team.' : 'Select a task to watch its agents.'} /></Suspense>
          <div className="notification-stack" aria-live="polite">
            {notifications.map((notification) => <div className={`notification ${notification.kind}`} key={notification.id}>
              <BellRing size={14} /><div><small>{notification.kind.replace('_', ' ').toUpperCase()}</small><p>{notification.message}</p></div>
              <button onClick={() => setNotifications((current) => current.filter((item) => item.id !== notification.id))} aria-label="Dismiss"><X size={13} /></button>
            </div>)}
          </div>
        </div>
      </section>

      <TaskPanel project={selectedProject} task={selectedTask} detail={detail} selectedAgent={agent}
        onSelectTask={setSelectedTaskId} onCreateTask={() => setModal('task')} onDeleteTask={(id) => void deleteTask(id)}
        onRun={(id) => void runTask(id)} onCancel={(id) => void cancelRun(id)} onCloseAgent={() => setSelectedAgent(null)} onOpenReport={() => setModal('report')}
        liveEvents={events} onSelectAgent={setSelectedAgent} predictions={book?.items ?? []} />

      <Suspense fallback={null}>
      {modal === 'project' && <ProjectModal onClose={() => setModal(null)} onCreate={(name, description) => void createProject(name, description)} />}
      {modal === 'task' && selectedProject && <TaskModal project={selectedProject} providers={providers} defaults={defaults} onClose={() => setModal(null)} onCreate={(title, description, config, files) => void createTask(title, description, config, files)} />}
      {modal === 'settings' && <SettingsModal onClose={() => setModal(null)} onSaved={setDefaults} />}
      {modal === 'report' && detail?.run.report && <ReportModal title={selectedTask?.title ?? 'Report'} report={detail.run.report} onClose={() => setModal(null)} />}
      </Suspense>
    </main>
  )
}

function LiveClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(new Date()), 1000)
    return () => window.clearInterval(timer)
  }, [])
  return <div className="utc">{now.toLocaleTimeString([], { hour12: false })}</div>
}

function EventRow({ event, onSelectAgent }: { event: SwarmEvent; onSelectAgent: (key: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const long = event.message.length > 140
  const who = event.from_agent_id ?? 'system'
  return <div className={`event-row kind-${event.kind} ${expanded ? 'expanded' : ''}`}>
    <time>{timeLabel(event.created_at)}</time>
    <button className="event-agent" title={who} onClick={() => event.from_agent_id && onSelectAgent(event.from_agent_id)}>{abbreviate(who)}</button>
    <p>{event.message}</p>
    {long && <button className="text-toggle" onClick={() => setExpanded((value) => !value)}>{expanded ? 'LESS' : 'MORE'}</button>}
  </div>
}

function abbreviate(key: string) {
  const parts = key.split('_').filter(Boolean)
  return (parts.length > 1 ? parts.map((part) => part[0]).join('') : key.slice(0, 3)).slice(0, 3).toUpperCase()
}
