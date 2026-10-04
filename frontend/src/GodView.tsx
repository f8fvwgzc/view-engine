import { memo, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Background, BackgroundVariant, BaseEdge, ControlButton, Controls, EdgeLabelRenderer, Handle, MarkerType, Position, ReactFlow, ReactFlowProvider,
  useInternalNode, useNodesState, useReactFlow,
  type Edge, type EdgeProps, type InternalNode, type Node, type NodeProps, type XYPosition,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { Activity, Eye, EyeOff, GitFork, LayoutGrid, Maximize, Network } from 'lucide-react'
import { formatTokens, type RunAgent, type RunDetail, type SwarmEvent } from './types'

const ACTIVE_WINDOW_MS = 5000
// Live view: an agent is visible while it works and for a few seconds after its last message, then it leaves the stage.
const LINGER_MS = 7000
const NODE_WIDTH = 216
const COLUMN_GAP = 300
const ROW_GAP = 132
const KIND_ORDER = ['orchestrator', 'intent', 'researcher', 'specialist', 'analyst', 'strategist', 'critic']

type Activity = { text: string; kind: string; searches: number; pages: number }
type AgentNodeData = { agent: RunAgent; activity: Activity | null; waitingFor: string[]; selected: boolean; leaving: boolean }
type AgentFlowNode = Node<AgentNodeData, 'agent'>
type Pulse = { id: string; reverse: boolean }
type LinkData = { kind: 'data' | 'org'; active: boolean; pulses: Pulse[]; label?: string }
type LinkEdge = Edge<LinkData, 'link'>

const nodeTypes = { agent: memo(AgentNode) }
const edgeTypes = { link: LinkEdgeView }

type Props = { detail: RunDetail | null; liveEvents: SwarmEvent[]; selectedAgent: string | null; onSelectAgent: (key: string | null) => void; emptyHint: string }

export function GodView(props: Props) {
  return <ReactFlowProvider><Canvas {...props} /></ReactFlowProvider>
}

function Canvas({ detail, liveEvents, selectedAgent, onSelectAgent, emptyHint }: Props) {
  const clock = useClock(900)
  const { fitView } = useReactFlow()
  const [nodes, setNodes, onNodesChange] = useNodesState<AgentFlowNode>([])
  const [mode, setMode] = useState<'data' | 'org'>('data')
  // The whole team is on stage by default (queued agents dimmed); 'live' keeps only agents that are working right now.
  const [view, setView] = useState<'all' | 'live'>('all')
  const dragged = useRef(new Set<string>())
  const allAgents = useMemo(() => detail?.agents ?? [], [detail])
  const live = view === 'live' && !!detail?.active
  // Live socket events arrive before the next run refresh; merge both, newest first, without duplicates.
  const runEvents = useMemo(() => {
    if (!detail) return []
    const seen = new Set<string>()
    return [...liveEvents.filter((event) => event.run_id === detail.run.id), ...detail.events].filter((event) => !seen.has(event.id) && !!seen.add(event.id))
  }, [detail, liveEvents])

  // When each agent last spoke or was spoken to: drives birth (appear on first activity) and exit (fade after LINGER_MS).
  const lastSeen = useMemo(() => {
    const map = new Map<string, number>()
    for (const event of runEvents) {
      const at = new Date(event.created_at).getTime()
      for (const key of [event.from_agent_id, event.to_agent_id]) if (key && !map.has(key)) map.set(key, at)
    }
    return map
  }, [runEvents])
  const agents = useMemo(() => live
    ? allAgents.filter((agent) => agent.key === 'orchestrator' || agent.key === selectedAgent || agent.status === 'running' || agent.status === 'retrying' || clock - (lastSeen.get(agent.key) ?? 0) < LINGER_MS)
    : allAgents, [live, allAgents, selectedAgent, lastSeen, clock])
  const visibleKey = agents.map((agent) => agent.key).join('|')
  // Positions come from the whole team so agents keep their place as they are born and leave.
  const layout = useMemo(() => dagLayout(allAgents, mode), [allAgents, mode])
  // Latest thing each agent did (search, page read, reasoning, progress) plus its running search/page counters.
  const activities = useMemo(() => {
    const map = new Map<string, Activity>()
    for (const event of runEvents) {
      const key = event.from_agent_id
      if (!key || map.has(key) || event.kind === 'agent_hired' || event.kind === 'agent_handoff') continue
      const counters = runEvents.find((item) => item.from_agent_id === key && item.kind === 'agent_activity')?.data as { searches?: number; pages?: number } | undefined
      map.set(key, { text: event.message, kind: (event.data?.activity as string) ?? event.kind, searches: counters?.searches ?? 0, pages: counters?.pages ?? 0 })
    }
    return map
  }, [runEvents])
  const statusByKey = useMemo(() => new Map(allAgents.map((agent) => [agent.key, agent.status])), [allAgents])

  useEffect(() => {
    setNodes((current) => {
      const previous = new Map(current.map((node) => [node.id, node]))
      return agents.map((agent) => {
        const existing = previous.get(agent.key)
        const leaving = live && agent.status !== 'running' && agent.status !== 'retrying' && agent.key !== 'orchestrator' && clock - (lastSeen.get(agent.key) ?? 0) > LINGER_MS - 2000
        const waitingFor = agent.status === 'pending' ? agent.depends_on.filter((dependency) => statusByKey.get(dependency) !== 'complete') : []
        const data = { agent, activity: activities.get(agent.key) ?? null, waitingFor, selected: agent.key === selectedAgent, leaving }
        const position = existing && dragged.current.has(agent.key) ? existing.position : layout.get(agent.key) ?? { x: 0, y: 0 }
        return existing ? { ...existing, position, data } : { id: agent.key, type: 'agent', position, data }
      })
    })
  }, [visibleKey, allAgents, layout, activities, statusByKey, selectedAgent, setNodes, live])

  const fit = useCallback(() => void fitView({ padding: 0.12, duration: 450, maxZoom: 1.1 }), [fitView])
  const measuredKey = nodes.length && nodes.every((node) => node.measured?.width) ? `${mode}|${nodes.map((node) => node.id).join('|')}` : ''
  useEffect(() => {
    if (!measuredKey) return
    const timer = window.setTimeout(fit, 60)
    return () => window.clearTimeout(timer)
  }, [measuredKey, fit])

  const edges = useMemo(() => buildEdges(agents, runEvents, clock, mode), [agents, runEvents, clock, mode])
  const born = allAgents.length
  const running = agents.filter((agent) => agent.status === 'running').length
  const latest = runEvents[0]

  function resetLayout() {
    dragged.current.clear()
    setNodes((current) => current.map((node) => ({ ...node, position: layout.get(node.id) ?? node.position })))
    window.setTimeout(fit, 30)
  }

  return <div className="god-view">
    <div className="canvas-caption">
      <span>{detail ? `${live ? `LIVE · ${agents.length} ON STAGE · ${born} BORN` : `${born} AGENTS`} · ROUND ${detail.run.round}` : 'NO RUN SELECTED'} <b>•</b> {mode === 'data' ? 'DATA FLOW' : 'ORG CHART'}</span>
      <span className={running ? 'caption-live' : ''}>{running ? `${running} WORKING` : detail?.run.status.toUpperCase() ?? 'IDLE'}</span>
    </div>
    <div className="flow-canvas">
      {!detail && <div className="canvas-empty">{emptyHint}</div>}
      <ReactFlow<AgentFlowNode, LinkEdge>
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        edgeTypes={edgeTypes}
        onNodesChange={onNodesChange}
        onNodeDragStop={(_, node) => { dragged.current.add(node.id) }}
        onNodeClick={(_, node) => onSelectAgent(node.id === selectedAgent ? null : node.id)}
        onPaneClick={() => onSelectAgent(null)}
        colorMode="dark"
        minZoom={0.25}
        maxZoom={2}
        nodesConnectable={false}
        edgesFocusable={false}
        elementsSelectable={false}
      >
        <Background variant={BackgroundVariant.Dots} gap={26} size={1} color="#2a302c" />
        <Controls showInteractive={false} showFitView={false} position="bottom-left">
          <ControlButton onClick={fit} title="Fit graph" aria-label="Fit graph"><Maximize size={12} /></ControlButton>
          <ControlButton onClick={resetLayout} title="Reset layout" aria-label="Reset layout"><LayoutGrid size={12} /></ControlButton>
          <ControlButton onClick={() => setView(live ? 'all' : 'live')} title={live ? 'Show the whole team' : 'Live: show agents only while they work'} aria-label="Toggle live view">{live ? <Eye size={12} /> : <EyeOff size={12} />}</ControlButton>
          <ControlButton onClick={() => setMode((value) => value === 'data' ? 'org' : 'data')} title={mode === 'data' ? 'Show org chart (who hired whom)' : 'Show data-flow graph (dependencies)'} aria-label="Toggle graph mode">{mode === 'data' ? <GitFork size={12} /> : <Network size={12} />}</ControlButton>
        </Controls>
      </ReactFlow>
    </div>
    <div className="canvas-footer"><Activity size={14} /><span>{latest?.message ?? (detail ? 'Waiting for the orchestrator…' : emptyHint)}</span></div>
  </div>
}

function AgentNode({ data }: NodeProps<AgentFlowNode>) {
  const { agent, activity, waitingFor, selected, leaving } = data
  const working = agent.status === 'running' || agent.status === 'retrying'
  const tokens = agent.tokens_in + agent.tokens_out + agent.tokens_cache_read
  return <article className={`agent-node ${agent.status} kind-${agent.kind} ${selected ? 'is-selected' : ''} ${leaving ? 'leaving' : ''}`} style={{ width: NODE_WIDTH }} title={agent.objective}>
    <Handle type="target" position={Position.Left} isConnectable={false} className="hidden-handle" />
    <Handle type="source" position={Position.Right} isConnectable={false} className="hidden-handle" />
    <header><span className="node-light" /><b>{agent.name}</b><em>{agent.status === 'pending' ? 'QUEUED' : agent.status.toUpperCase()}</em></header>
    <small><span className={`kind-badge kind-${agent.kind}`}>{agent.kind}</span>{agent.role}</small>
    {agent.skills.length > 0 && <div className="node-skills">{agent.skills.slice(0, 3).map((skill) => <span key={skill}>{skill}</span>)}</div>}
    <div className="node-stats">
      {agent.kind !== 'orchestrator' && <span title="Iterations">⟳ {agent.iterations}/{agent.max_iterations}</span>}
      {tokens > 0 && <span title="Tokens (in + out + cache read)">◇ {formatTokens(tokens)}</span>}
      <span title="Provider and model (from the model router)">{agent.provider}{agent.model ? `:${agent.model}` : ''}</span>
      {agent.attempt > 1 && <span title="Attempts">↻ {agent.attempt}</span>}
      {activity && activity.searches > 0 && <span title="Web searches">⌕ {activity.searches}</span>}
      {activity && activity.pages > 0 && <span title="Pages read">▤ {activity.pages}</span>}
    </div>
    {working && activity && <p className={`node-message activity-${activity.kind}`}><i className="activity-dot" />{activity.text}</p>}
    {waitingFor.length > 0 && <p className="node-waiting">waiting for {waitingFor.join(', ')}</p>}
  </article>
}

function LinkEdgeView({ id, source, target, data, markerEnd }: EdgeProps<LinkEdge>) {
  const sourceNode = useInternalNode(source)
  const targetNode = useInternalNode(target)
  if (!sourceNode || !targetNode || !data) return null
  const start = anchor(sourceNode, 'right')
  const end = anchor(targetNode, 'left')
  // Left-to-right S-curves; edges that must go backwards loop out wider so they do not cut through cards.
  const backwards = end.x < start.x + 20
  const bend = backwards ? 140 : Math.max(40, (end.x - start.x) * 0.5)
  const c1 = { x: start.x + bend, y: start.y }
  const c2 = { x: end.x - bend, y: end.y }
  const path = `M ${start.x} ${start.y} C ${c1.x} ${c1.y}, ${c2.x} ${c2.y}, ${end.x} ${end.y}`
  const label = { x: (start.x + 3 * c1.x + 3 * c2.x + end.x) / 8, y: (start.y + 3 * c1.y + 3 * c2.y + end.y) / 8 }
  return <>
    <BaseEdge id={id} path={path} markerEnd={markerEnd} className={`link-path ${data.kind} ${data.active ? 'active' : ''}`} />
    {data.pulses.map((pulse) => <circle key={pulse.id} r={3.4} className="message-pulse">
      <animateMotion dur="1.2s" repeatCount="indefinite" path={path} keyPoints={pulse.reverse ? '1;0' : '0;1'} keyTimes="0;1" calcMode="linear" />
    </circle>)}
    {data.label && data.active && <EdgeLabelRenderer>
      <div className="edge-badge active" style={{ transform: `translate(-50%, -50%) translate(${label.x}px, ${label.y}px)` }}>{data.label}</div>
    </EdgeLabelRenderer>}
  </>
}

function anchor(node: InternalNode, side: 'left' | 'right'): XYPosition {
  const { x, y } = node.internals.positionAbsolute
  const width = node.measured.width ?? NODE_WIDTH
  return { x: side === 'right' ? x + width + 4 : x - 4, y: y + (node.measured.height ?? 90) / 2 }
}

const EDGE_LABELS: Record<string, string> = { agent_handoff: 'data', agent_hired: 'hired', agent_started: 'dispatch', agent_result: 'report' }

function buildEdges(agents: RunAgent[], events: SwarmEvent[], clock: number, mode: 'data' | 'org'): LinkEdge[] {
  const keys = new Set(agents.map((agent) => agent.key))
  const edges = new Map<string, LinkEdge>()
  const add = (source: string, target: string, kind: 'data' | 'org') => {
    if (!keys.has(source) || !keys.has(target) || source === target) return
    const id = `${source}->${target}`
    if (!edges.has(id)) edges.set(id, { id, source, target, type: 'link', markerEnd: { type: MarkerType.ArrowClosed, width: 13, height: 13, color: '#5d665f' }, data: { kind, active: false, pulses: [] } })
  }
  for (const agent of agents) {
    if (mode === 'data') {
      if (agent.depends_on.length) agent.depends_on.forEach((dependency) => add(dependency, agent.key, 'data'))
      else if (agent.key !== 'orchestrator') add('orchestrator', agent.key, 'org')
    } else if (agent.hired_by) {
      add(agent.hired_by, agent.key, 'org')
    }
  }
  for (const event of events) {
    if (clock - new Date(event.created_at).getTime() > ACTIVE_WINDOW_MS) continue
    const from = event.from_agent_id; const to = event.to_agent_id
    if (!from || !to) continue
    const forward = edges.get(`${from}->${to}`)
    const backward = edges.get(`${to}->${from}`)
    const edge = forward ?? backward
    if (!edge?.data || edge.data.pulses.length >= 2) continue
    edge.data.active = true
    edge.data.label = EDGE_LABELS[event.kind]
    edge.data.pulses.push({ id: event.id, reverse: !forward })
  }
  return [...edges.values()]
}

/** Layered DAG layout: column = longest dependency path (data mode) or hiring depth (org mode); rows centred per column. */
function dagLayout(agents: RunAgent[], mode: 'data' | 'org'): Map<string, XYPosition> {
  const byKey = new Map(agents.map((agent) => [agent.key, agent]))
  const depth = new Map<string, number>()
  const visit = (key: string, trail: Set<string>): number => {
    if (depth.has(key)) return depth.get(key)!
    const agent = byKey.get(key)
    if (!agent || trail.has(key)) return 0
    trail.add(key)
    let value: number
    if (key === 'orchestrator') value = 0
    else if (mode === 'data') value = agent.depends_on.length ? Math.max(...agent.depends_on.map((dependency) => visit(dependency, trail))) + 1 : 1
    else value = agent.hired_by && byKey.has(agent.hired_by) ? visit(agent.hired_by, trail) + 1 : 1
    trail.delete(key)
    depth.set(key, value)
    return value
  }
  agents.forEach((agent) => visit(agent.key, new Set()))
  const columns = new Map<number, RunAgent[]>()
  for (const agent of agents) {
    const column = depth.get(agent.key) ?? 0
    columns.set(column, [...(columns.get(column) ?? []), agent])
  }
  const positions = new Map<string, XYPosition>()
  for (const [column, members] of columns) {
    members.sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind) || a.round - b.round || a.created_at.localeCompare(b.created_at))
    members.forEach((agent, index) => positions.set(agent.key, { x: column * COLUMN_GAP, y: (index - (members.length - 1) / 2) * ROW_GAP }))
  }
  return positions
}

function useClock(intervalMs: number) {
  const [now, setNow] = useState(Date.now())
  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), intervalMs)
    return () => window.clearInterval(timer)
  }, [intervalMs])
  return now
}
