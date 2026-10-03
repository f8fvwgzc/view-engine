import { FormEvent, useEffect, useMemo, useState } from 'react'
import { Activity, BellRing, Bot, ChevronRight, CircleDot, FolderPlus, Play, Plus, Radio, X } from 'lucide-react'

const API = import.meta.env.VITE_API_URL ?? ''
const websocketUrl = `${window.location.protocol === 'https:' ? 'wss' : 'ws'}://${window.location.host}/ws`

type AgentStatus = 'idle' | 'working' | 'waiting' | 'complete' | 'error'
type TaskStatus = 'draft' | 'ready' | 'running' | 'complete' | 'failed'

type Agent = { id: string; name: string; role: string; description: string; status: AgentStatus; active_task_id: string | null; x: number; y: number }
type Task = { id: string; title: string; description: string; assigned_agent_ids: string[]; status: TaskStatus; result: string | null; completed_at: string | null; run_started_at: string | null; created_at: string }
type Project = { id: string; name: string; description: string; created_at: string; tasks: Task[] }
type SwarmEvent = { id: string; project_id: string; task_id: string | null; kind: string; message: string; from_agent_id: string | null; to_agent_id: string | null; created_at: string }
type Dashboard = { projects: Project[]; agents: Agent[]; events: SwarmEvent[] }
type Notification = Pick<SwarmEvent, 'id' | 'kind' | 'message' | 'from_agent_id' | 'to_agent_id'>

const agentLinks = [
  ['orchestrator', 'planner'], ['orchestrator', 'researcher'], ['orchestrator', 'builder'], ['orchestrator', 'reviewer'],
  ['planner', 'researcher'], ['planner', 'builder'], ['planner', 'reviewer'],
  ['researcher', 'planner'], ['researcher', 'builder'], ['researcher', 'reviewer'],
  ['builder', 'planner'], ['builder', 'researcher'], ['builder', 'reviewer'],
  ['reviewer', 'planner'], ['reviewer', 'builder'], ['reviewer', 'orchestrator'],
]

function statusLabel(status: string) { return status === 'idle' ? 'STANDBY' : status.toUpperCase() }
function timeLabel(iso: string) { return new Date(iso).toLocaleTimeString([], { hour12: false, second: '2-digit' }) }

export function App() {
  const [projects, setProjects] = useState<Project[]>([])
  const [agents, setAgents] = useState<Agent[]>([])
  const [events, setEvents] = useState<SwarmEvent[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(null)
  const [selectedTaskId, setSelectedTaskId] = useState<string | null>(null)
  const [showProjectForm, setShowProjectForm] = useState(false)
  const [showTaskForm, setShowTaskForm] = useState(false)
  const [connected, setConnected] = useState(false)
  const [refreshing, setRefreshing] = useState(false)
  const [notifications, setNotifications] = useState<Notification[]>([])
  const [clock, setClock] = useState(Date.now())

  const selectedProject = projects.find((project) => project.id === selectedProjectId) ?? null
  const selectedTask = selectedProject?.tasks.find((task) => task.id === selectedTaskId) ?? null
  const runningAgents = agents.filter((agent) => agent.status === 'working').length
  const selectedEvents = useMemo(() => events.filter((event) => !selectedProjectId || event.project_id === selectedProjectId), [events, selectedProjectId])

  async function loadDashboard() {
    setRefreshing(true)
    try {
      const dashboard: Dashboard = await fetch(`${API}/api/dashboard`).then((response) => response.json())
      setProjects(dashboard.projects)
      setAgents(dashboard.agents)
      setEvents(dashboard.events)
      setSelectedProjectId((current) => current && dashboard.projects.some((project) => project.id === current) ? current : dashboard.projects[0]?.id ?? null)
    } finally { setRefreshing(false) }
  }

  useEffect(() => { void loadDashboard() }, [])
  useEffect(() => {
    const interval = window.setInterval(() => setClock(Date.now()), 700)
    return () => window.clearInterval(interval)
  }, [])

  useEffect(() => {
    const socket = new WebSocket(websocketUrl)
    socket.onopen = () => setConnected(true)
    socket.onclose = () => setConnected(false)
    socket.onmessage = (message) => {
      const event: SwarmEvent = JSON.parse(message.data)
      setEvents((current) => [event, ...current].slice(0, 120))
      setNotifications((current) => [{ id: event.id, kind: event.kind, message: event.message, from_agent_id: event.from_agent_id, to_agent_id: event.to_agent_id }, ...current.filter((notification) => notification.id !== event.id)].slice(0, 4))
      window.setTimeout(() => setNotifications((current) => current.filter((notification) => notification.id !== event.id)), 6000)
      window.setTimeout(() => void loadDashboard(), 40)
    }
    return () => socket.close()
  }, [])

  async function createProject(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const project = await fetch(`${API}/api/projects`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ name: form.get('name'), description: form.get('description') }) }).then((response) => response.json()) as Project
    setProjects((current) => [...current, project])
    setSelectedProjectId(project.id)
    setSelectedTaskId(null)
    setShowProjectForm(false)
  }

  async function createTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!selectedProjectId) return
    const form = new FormData(event.currentTarget)
    const assigned_agent_ids = form.getAll('agents').map(String)
    const task = await fetch(`${API}/api/projects/${selectedProjectId}/tasks`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: form.get('title'), description: form.get('description'), assigned_agent_ids }) }).then((response) => response.json()) as Task
    setProjects((current) => current.map((project) => project.id === selectedProjectId ? { ...project, tasks: [...project.tasks, task] } : project))
    setSelectedTaskId(task.id)
    setShowTaskForm(false)
  }

  async function runTask(taskId: string) {
    await fetch(`${API}/api/tasks/${taskId}/run`, { method: 'POST' })
    await loadDashboard()
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div className="brand"><span className="brand-mark">◉</span> GODVIEW <span>ENGINE</span></div>
        <div className="system-readout">
          <span>SYS <b>LOCAL</b></span><i />
          <span>AGENTS <b>{runningAgents}/{agents.length}</b></span><i />
          <span>EVENTS <b>{events.length}</b></span><i />
          <span>NET <b className={connected ? 'ok' : 'bad'}>{connected ? 'SYNCED' : 'OFFLINE'}</b></span>
        </div>
        <div className="utc">{new Date().toLocaleTimeString([], { hour12: false })} LOCAL</div>
      </header>

      <aside className="left-panel">
        <section className="panel-section project-heading">
          <div className="section-label"><CircleDot size={13} /> PROJECTS</div>
          <button className="icon-button" onClick={() => setShowProjectForm(true)} aria-label="Create project"><FolderPlus size={16} /></button>
        </section>
        <div className="project-list">
          {projects.length === 0 && <div className="empty">Create a project to begin.</div>}
          {projects.map((project) => <button key={project.id} className={`project-row ${project.id === selectedProjectId ? 'selected' : ''}`} onClick={() => { setSelectedProjectId(project.id); setSelectedTaskId(null) }}>
            <span><b>{project.name}</b><small>{project.tasks.length} task{project.tasks.length === 1 ? '' : 's'}</small></span><ChevronRight size={14} />
          </button>)}
        </div>

        <section className="panel-section stream-heading"><div className="section-label"><Radio size={13} /> EVENT STREAM</div><span className="live-dot">LIVE</span></section>
        <div className="event-stream">
          {selectedEvents.length === 0 && <div className="empty">Agent telemetry appears here during a run.</div>}
          {selectedEvents.slice(0, 40).map((event) => <div className="event-row" key={event.id}>
            <time>{timeLabel(event.created_at)}</time><span>{event.from_agent_id?.slice(0, 3).toUpperCase() ?? 'SYS'}</span><p>{event.message}</p>
          </div>)}
        </div>
      </aside>

      <section className="workspace">
        <div className="workspace-toolbar">
          <div><p className="eyebrow">ACTIVE PROJECT</p><h1>{selectedProject?.name ?? 'No project selected'}</h1></div>
          <div className="metrics"><Metric label="TASKS" value={selectedProject?.tasks.length ?? 0} /><Metric label="WORKING" value={runningAgents} /><Metric label="STATUS" value={refreshing ? 'SYNCING' : connected ? 'READY' : 'WAIT'} /></div>
        </div>

        <GodView agents={agents} events={events} clock={clock} selectedTask={selectedTask} />
      </section>

      <aside className="right-panel">
        <section className="task-header"><div><p className="eyebrow">WORK QUEUE</p><h2>{selectedProject ? 'Tasks' : 'Select a project'}</h2></div>{selectedProject && <button className="icon-button" onClick={() => setShowTaskForm(true)} aria-label="Create task"><Plus size={17} /></button>}</section>
        <div className="task-list">
          {selectedProject?.tasks.map((task) => <button key={task.id} className={`task-card ${task.id === selectedTaskId ? 'selected' : ''}`} onClick={() => setSelectedTaskId(task.id)}>
            <div><span className={`status-dot ${task.status}`} /><span className="task-status">{task.status}</span></div><b>{task.title}</b><small>{task.assigned_agent_ids.length ? `${task.assigned_agent_ids.length} agents selected` : 'orchestrator decides on run'}</small>
          </button>)}
          {selectedProject && selectedProject.tasks.length === 0 && <div className="empty">No tasks yet. Add one to send the swarm to work.</div>}
        </div>
        {selectedTask && <TaskInspector task={selectedTask} agents={agents} onRun={() => void runTask(selectedTask.id)} />}
      </aside>

      {showProjectForm && <ProjectModal onClose={() => setShowProjectForm(false)} onSubmit={createProject} />}
      {showTaskForm && selectedProject && <TaskModal project={selectedProject} onClose={() => setShowTaskForm(false)} onSubmit={createTask} />}
      <div className="notification-stack" aria-live="polite">
        {notifications.map((notification) => <div className={`notification ${notification.kind}`} key={notification.id}>
          <BellRing size={14} /><div><small>{notification.from_agent_id?.toUpperCase() ?? 'SYSTEM'}{notification.to_agent_id ? ` → ${notification.to_agent_id.toUpperCase()}` : ''}</small><p>{notification.message}</p></div><button onClick={() => setNotifications((current) => current.filter((item) => item.id !== notification.id))}><X size={13} /></button>
        </div>)}
      </div>
    </main>
  )
}

function Metric({ label, value }: { label: string; value: string | number }) { return <span><small>{label}</small><b>{value}</b></span> }

function GodView({ agents, events, clock, selectedTask }: { agents: Agent[]; events: SwarmEvent[]; clock: number; selectedTask: Task | null }) {
  const taskEvents = selectedTask ? events.filter((event) => event.task_id === selectedTask.id && (!selectedTask.run_started_at || new Date(event.created_at).getTime() >= new Date(selectedTask.run_started_at).getTime())) : []
  const latestEvent = taskEvents[0]
  const agentById = new Map(agents.map((agent) => [agent.id, agent]))
  const visibleAgentIds = new Set(['orchestrator'])
  if (selectedTask?.status === 'complete') selectedTask.assigned_agent_ids.forEach((agentId) => visibleAgentIds.add(agentId))
  taskEvents.forEach((event) => { if (event.from_agent_id) visibleAgentIds.add(event.from_agent_id); if (event.to_agent_id) visibleAgentIds.add(event.to_agent_id) })
  const visibleAgents = agents.filter((agent) => visibleAgentIds.has(agent.id))
  const activeEvents = taskEvents.filter((event) => event.from_agent_id && event.to_agent_id && clock - new Date(event.created_at).getTime() < 4600 && agentById.has(event.from_agent_id) && agentById.has(event.to_agent_id)).slice(0, 12)
  const activeLinks = new Set(activeEvents.map((event) => `${event.from_agent_id}-${event.to_agent_id}`))
  return <div className="god-view">
    <div className="grid-overlay" />
    <div className="canvas-caption">LIVE AGENT TOPOLOGY <span>•</span> SELECTED SWARM</div>
    <svg className="links" viewBox="0 0 100 100" preserveAspectRatio="none">
      {agentLinks.map(([fromId, toId]) => {
        const from = agentById.get(fromId); const to = agentById.get(toId)
        if (!from || !to || !visibleAgentIds.has(fromId) || !visibleAgentIds.has(toId)) return null
        const isActive = activeLinks.has(`${fromId}-${toId}`)
        return <path key={`${fromId}-${toId}`} className={isActive ? 'active-link' : ''} d={`M ${from.x} ${from.y} L ${to.x} ${to.y}`} />
      })}
      {activeEvents.map((event) => {
        const from = agentById.get(event.from_agent_id!); const to = agentById.get(event.to_agent_id!)
        if (!from || !to) return null
        return <circle className="message-pulse" key={event.id} r="0.72"><animateMotion dur="1.05s" repeatCount="indefinite" path={`M ${from.x} ${from.y} L ${to.x} ${to.y}`} /></circle>
      })}
    </svg>
    {visibleAgents.map((agent) => <article key={agent.id} style={{ left: `${agent.x}%`, top: `${agent.y}%` }} className={`agent-node ${agent.status}`}>
      <span className="node-light" /><div><b>{agent.name}</b><small>{agent.role}</small></div><em>{statusLabel(agent.status)}</em>
    </article>)}
    <div className="canvas-footer"><Activity size={14} /><span>{latestEvent?.message ?? (selectedTask ? 'Orchestrator is ready to choose this task’s swarm.' : 'Select a task to view its swarm.')}</span></div>
  </div>
}

function TaskInspector({ task, agents, onRun }: { task: Task; agents: Agent[]; onRun: () => void }) {
  const assigned = agents.filter((agent) => task.assigned_agent_ids.length === 0 || task.assigned_agent_ids.includes(agent.id))
  return <div className="task-inspector"><div className="inspector-title"><Bot size={16} /><span>SELECTED TASK</span></div><h3>{task.title}</h3><p>{task.description || 'No description added.'}</p><div className="agent-chips">{assigned.length ? assigned.map((agent) => <span key={agent.id}>{agent.name}</span>) : <span className="orchestrator-note">ORCHESTRATOR DECIDES ON RUN</span>}</div>{task.result && <div className="swarm-result"><span>SWARM RESULT</span><p>{task.result}</p>{task.completed_at && <small>Completed {timeLabel(task.completed_at)}</small>}</div>}<button disabled={task.status === 'running'} className="run-button" onClick={onRun}><Play size={14} fill="currentColor" />{task.status === 'running' ? 'SWARM RUNNING' : task.result ? 'RUN AGAIN' : 'RUN SWARM'}</button></div>
}

function ProjectModal({ onClose, onSubmit }: { onClose: () => void; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <div className="modal-layer"><form className="modal" onSubmit={onSubmit}><button type="button" className="modal-close" onClick={onClose}><X size={17} /></button><p className="eyebrow">NEW CONTAINER</p><h2>Create project</h2><label>PROJECT NAME<input name="name" placeholder="Launch website" required autoFocus /></label><label>CONTEXT<textarea name="description" placeholder="What is this project for?" rows={3} /></label><button className="primary" type="submit">CREATE PROJECT</button></form></div> }

function TaskModal({ project, onClose, onSubmit }: { project: Project; onClose: () => void; onSubmit: (event: FormEvent<HTMLFormElement>) => void }) { return <div className="modal-layer"><form className="modal" onSubmit={onSubmit}><button type="button" className="modal-close" onClick={onClose}><X size={17} /></button><p className="eyebrow">{project.name.toUpperCase()}</p><h2>Create task</h2><label>TASK TITLE<input name="title" placeholder="Build the landing page" required autoFocus /></label><label>BRIEF<textarea name="description" placeholder="Give the orchestrator useful context." rows={3} /></label><p className="orchestrator-copy">The orchestrator reads this brief, selects the right workers, then reveals them in the live swarm as it dispatches each one.</p><button className="primary" type="submit">CREATE TASK</button></form></div> }
