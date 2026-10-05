import { useState } from 'react'
import { Bot, ExternalLink, Maximize2, Play, Plus, Square, Trash2, X } from 'lucide-react'
import { agentReport, formatTokens, stripJsonBlock, timeLabel, type Project, type RunAgent, type RunDetail, type Prediction, type SwarmEvent, type Task } from '../types'
import { Markdown } from './Markdown'
import { ChartButton, ChartThumbs, MarketPack, TradePlanCard } from './Trading'

type Props = {
  project: Project | null
  task: Task | null
  detail: RunDetail | null
  selectedAgent: RunAgent | null
  onSelectTask: (id: string | null) => void
  onCreateTask: () => void
  onDeleteTask: (id: string) => void
  onRun: (id: string) => void
  onCancel: (runId: string) => void
  onCloseAgent: () => void
  onOpenReport: () => void
  liveEvents: SwarmEvent[]
  onSelectAgent: (key: string) => void
  predictions: Prediction[]
}

export function TaskPanel({ project, task, detail, selectedAgent, onSelectTask, onCreateTask, onDeleteTask, onRun, onCancel, onCloseAgent, onOpenReport, liveEvents, onSelectAgent, predictions }: Props) {
  return <aside className="right-panel">
    <section className="task-header">
      <div><p className="eyebrow">RESEARCH QUEUE</p><h2>{project ? 'Tasks' : 'Select a project'}</h2></div>
      {project && <button className="icon-button" onClick={onCreateTask} aria-label="New research task"><Plus size={17} /></button>}
    </section>
    <div className="task-list">
      {project?.tasks.map((item) => <div key={item.id} className={`task-card ${item.id === task?.id ? 'selected' : ''}`} role="button" tabIndex={0} onClick={() => onSelectTask(item.id)} onKeyDown={(event) => { if (event.key === 'Enter') onSelectTask(item.id) }}>
        <div className="task-card-top"><span className={`status-dot ${item.status}`} /><span className="task-status">{item.status}</span>{item.config.depth && <span className="task-chip">{item.config.depth}</span>}{item.config.provider && <span className="task-chip">{item.config.provider}</span>}{item.config.as_of && <span className="task-chip replay">replay {new Date(item.config.as_of).toLocaleDateString([], { month: 'short', day: 'numeric', timeZone: 'UTC' })}</span>}{item.config.style && <span className="task-chip">{item.config.style}</span>}
          <button className="ghost-icon" onClick={(event) => { event.stopPropagation(); if (window.confirm(`Delete task “${item.title}” and its runs?`)) onDeleteTask(item.id) }} aria-label="Delete task"><Trash2 size={12} /></button>
        </div>
        <b>{item.title}</b>
        {item.result && <small className="task-result">{item.result}</small>}
      </div>)}
      {project && project.tasks.length === 0 && <div className="empty">No tasks yet. Ask a question and the orchestrator will hire a research team for it.</div>}
    </div>
    {task && (selectedAgent
      ? <AgentDetail agent={selectedAgent} onClose={onCloseAgent} />
      : <Inspector task={task} detail={detail} liveEvents={liveEvents} onSelectAgent={onSelectAgent} prediction={predictions.find((item) => item.task_id === task.id) ?? null} onRun={onRun} onCancel={onCancel} onClose={() => onSelectTask(null)} onOpenReport={onOpenReport} />)}
  </aside>
}

function Inspector({ task, detail, liveEvents, onSelectAgent, prediction, onRun, onCancel, onClose, onOpenReport }: { task: Task; detail: RunDetail | null; liveEvents: SwarmEvent[]; onSelectAgent: (key: string) => void; prediction: Prediction | null; onRun: (id: string) => void; onCancel: (runId: string) => void; onClose: () => void; onOpenReport: () => void }) {
  const [tab, setTab] = useState<'decision' | 'report'>('decision')
  const run = detail?.run.task_id === task.id ? detail.run : null
  const active = !!detail?.active && run !== null
  const decision = run?.decision
  return <div className="task-inspector">
    <div className="inspector-title"><Bot size={16} /><span>{run ? `RUN · ${run.status.toUpperCase()}${run.round > 1 ? ` · ROUND ${run.round}` : ''}` : 'NOT RUN YET'}</span><button onClick={onClose} aria-label="Close task"><X size={13} /></button></div>
    <div className="inspector-body">
      <h3>{task.title}</h3>
      {task.description && <p>{task.description}</p>}
      <ChartThumbs taskId={task.id} />
      {task.config.symbol && task.config.mode === 'trading' && <ChartButton symbol={task.config.symbol} interval={task.config.routing?.charts?.charts?.[0]?.timeframe ?? (task.config.style === 'swing' ? '4h' : '15m')} asOf={task.config.as_of} />}
      {run?.error && <div className="run-error">{run.error}</div>}
      {run && <div className="tabs"><button className={tab === 'decision' ? 'active' : ''} onClick={() => setTab('decision')}>DECISION</button><button className={tab === 'report' ? 'active' : ''} onClick={() => setTab('report')} disabled={!run.report}>REPORT</button>{run.report && <button className="tab-action" onClick={onOpenReport} title="Open full report"><Maximize2 size={12} /></button>}</div>}
      {run && tab === 'decision' && <>
        {decision?.trade_plan && <TradePlanCard plan={decision.trade_plan} run={run} prediction={prediction} asOf={task.config.as_of} />}
        {detail && <TeamRoster detail={detail} liveEvents={liveEvents} onSelectAgent={onSelectAgent} />}
        <MarketPack run={run} />
        {run.intent?.decision_to_make && <div className="intent-card"><span>DECISION TO MAKE</span><p>{run.intent.decision_to_make}</p>
          {run.intent.intent?.success_criteria?.length ? <ul>{run.intent.intent.success_criteria.map((criterion) => <li key={criterion}>{criterion}</li>)}</ul> : null}
        </div>}
        {decision?.recommendation && <div className="decision-card">
          <span>RECOMMENDATION</span>
          <p>{decision.recommendation}</p>
          {typeof decision.confidence === 'number' && <div className="confidence" title="Strategist confidence"><i style={{ width: `${Math.round(decision.confidence * 100)}%` }} /><small>{Math.round(decision.confidence * 100)}% confidence{decision.verdict ? ` · critic: ${decision.verdict}` : ''}</small></div>}
          {decision.options?.length ? <table className="options"><thead><tr><th>Option</th><th>Score</th></tr></thead><tbody>{decision.options.map((option) => <tr key={option.name}><td><b>{option.name}</b>{option.pros?.length ? <small>+ {option.pros.join('; ')}</small> : null}{option.cons?.length ? <small>− {option.cons.join('; ')}</small> : null}</td><td>{option.score ?? '–'}</td></tr>)}</tbody></table> : null}
          {decision.next_actions?.length ? <><span>NEXT ACTIONS</span><ol>{decision.next_actions.map((action, index) => <li key={index}>{action.action}{action.owner || action.when ? <small> — {[action.owner, action.when].filter(Boolean).join(', ')}</small> : null}</li>)}</ol></> : null}
          {decision.flip_conditions?.length ? <><span>WOULD FLIP IF</span><ul>{decision.flip_conditions.map((condition) => <li key={condition}>{condition}</li>)}</ul></> : null}
        </div>}
        {!decision?.recommendation && active && <div className="empty">The team is researching. The decision appears here when the strategist finishes.</div>}
      </>}
      {run && tab === 'report' && run.report && <Markdown text={run.report} className="report-inline" />}
      {run && <small className="run-meta">{run.provider}{run.model ? ` · ${run.model}` : ''} · started {timeLabel(run.started_at)}{run.completed_at ? ` · finished ${timeLabel(run.completed_at)}` : ''}</small>}
    </div>
    <div className="inspector-actions">
      {active && run
        ? <button className="run-button danger" onClick={() => onCancel(run.id)}><Square size={13} fill="currentColor" />CANCEL RUN</button>
        : <button className="run-button" onClick={() => onRun(task.id)}><Play size={14} fill="currentColor" />{run ? 'RUN AGAIN' : 'START RESEARCH'}</button>}
    </div>
  </div>
}

const STATUS_LABEL: Record<string, string> = { pending: 'queued', running: 'working', retrying: 'retrying', complete: 'done', failed: 'failed', skipped: 'skipped' }

/** Every agent of the run with its live state and what it is doing right now. */
function TeamRoster({ detail, liveEvents, onSelectAgent }: { detail: RunDetail; liveEvents: SwarmEvent[]; onSelectAgent: (key: string) => void }) {
  const latest = new Map<string, string>()
  for (const event of [...liveEvents.filter((item) => item.run_id === detail.run.id), ...detail.events]) {
    if (event.from_agent_id && !latest.has(event.from_agent_id) && event.kind !== 'agent_hired') latest.set(event.from_agent_id, event.message)
  }
  const done = detail.agents.filter((agent) => agent.status === 'complete').length
  return <div className="team-roster">
    <span>TEAM · {done}/{detail.agents.length} DONE</span>
    {detail.agents.map((agent) => <button key={agent.id} className="roster-row" onClick={() => onSelectAgent(agent.key)} title={agent.objective}>
      <i className={`roster-dot ${agent.status}`} /><b>{agent.name}</b><em>{STATUS_LABEL[agent.status] ?? agent.status}</em>
      <small>{agent.status === 'pending' ? `waits for ${agent.depends_on.join(', ') || 'a free slot'}` : latest.get(agent.key) ?? agent.role}</small>
    </button>)}
  </div>
}

function AgentDetail({ agent, onClose }: { agent: RunAgent; onClose: () => void }) {
  const report = agentReport(agent) as { key_findings?: { claim: string; evidence?: string; source?: string; confidence?: number }[]; open_questions?: string[]; risks?: string[] } | null
  const body = stripJsonBlock(agent.output ?? '').trim()
  return <div className="task-inspector">
    <div className="inspector-title"><Bot size={16} /><span>AGENT · {agent.kind.toUpperCase()}</span><button onClick={onClose} aria-label="Close agent"><X size={13} /></button></div>
    <div className="inspector-body">
      <h3>{agent.name}</h3>
      <p>{agent.role}</p>
      <div className="agent-meta">
        <span className={`status-pill ${agent.status}`}>{agent.status}</span>
        <span>⟳ {agent.iterations}/{agent.max_iterations}</span>
        <span>{agent.provider}{agent.model ? `:${agent.model}` : ''}</span>
        <span>◇ {formatTokens(agent.tokens_in + agent.tokens_out)} · cache {formatTokens(agent.tokens_cache_read)}</span>
        {agent.attempt > 1 && <span>attempt {agent.attempt}</span>}
      </div>
      <div className="intent-card"><span>OBJECTIVE</span><p>{agent.objective}</p></div>
      <div className="agent-chips">
        {agent.hired_by && <span title="Hired by">hired by {agent.hired_by}</span>}
        {agent.reports_to && <span title="Reports to">reports to {agent.reports_to}</span>}
        {agent.depends_on.map((dependency) => <span key={dependency} title="Depends on">← {dependency}</span>)}
        {agent.skills.map((skill) => <span key={skill} className="skill-chip">✦ {skill}</span>)}
      </div>
      {agent.error && <div className="run-error">{agent.error}</div>}
      {report?.key_findings?.length ? <div className="findings"><span>KEY FINDINGS</span>{report.key_findings.map((finding, index) => <div key={index} className="finding">
        <p>{finding.claim}{finding.evidence ? <em> — {finding.evidence}</em> : null}</p>
        <small>{typeof finding.confidence === 'number' ? `${Math.round(finding.confidence * 100)}% · ` : ''}{finding.source?.startsWith('http') ? <a href={finding.source} target="_blank" rel="noreferrer noopener">{new URL(finding.source).hostname} <ExternalLink size={10} /></a> : finding.source}</small>
      </div>)}</div> : null}
      {report?.open_questions?.length ? <div className="intent-card"><span>OPEN QUESTIONS</span><ul>{report.open_questions.map((question) => <li key={question}>{question}</li>)}</ul></div> : null}
      {body ? <Markdown text={body} className="report-inline" /> : <div className="empty">{agent.status === 'pending' ? 'Waiting for its dependencies.' : agent.status === 'running' ? 'Working…' : 'No output.'}</div>}
    </div>
  </div>
}
