import type { Backtest, SkillImport, WatchItem, Attachment, Dashboard, MarketSymbol, MemoryItem, PredictionBook, Project, Provider, Run, RunDetail, Settings, Skill, Task, TaskConfig } from './types'

const API = import.meta.env.VITE_API_URL ?? ''

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API}${path}`, { ...init, headers: { 'Content-Type': 'application/json', ...init?.headers } })
  if (!response.ok) throw new Error(`${init?.method ?? 'GET'} ${path} failed: ${response.status} ${await response.text()}`)
  const body = await response.text()
  return (body ? JSON.parse(body) : undefined) as T
}

async function upload(taskId: string, files: File[]): Promise<Attachment[]> {
  const form = new FormData()
  files.forEach((file) => form.append('file', file, file.name))
  const response = await fetch(`${API}/api/tasks/${taskId}/attachments`, { method: 'POST', body: form })
  if (!response.ok) throw new Error(`Upload failed: ${response.status} ${await response.text()}`)
  return response.json()
}

export const attachmentUrl = (id: string) => `${API}/api/attachments/${id}`

export const api = {
  uploadAttachments: upload,
  attachments: (taskId: string) => request<Attachment[]>(`/api/tasks/${taskId}/attachments`),
  deleteAttachment: (id: string) => request<void>(`/api/attachments/${id}`, { method: 'DELETE' }),
  watchlist: (projectId: string) => request<WatchItem[]>(`/api/projects/${projectId}/watchlist`),
  addWatch: (projectId: string, symbol: string, intervals: string[]) => request<WatchItem>(`/api/projects/${projectId}/watchlist`, { method: 'POST', body: JSON.stringify({ symbol, intervals }) }),
  deleteWatch: (id: string) => request<void>(`/api/watchlist/${id}`, { method: 'DELETE' }),
  predictions: (projectId: string) => request<PredictionBook>(`/api/predictions?project_id=${projectId}`),
  marketSymbols: () => request<MarketSymbol[] | { symbols: MarketSymbol[] }>('/api/market/symbols').then((value) => Array.isArray(value) ? value : value.symbols ?? []),
  dashboard: () => request<Dashboard>('/api/dashboard'),
  createProject: (name: string, description: string) => request<Project>('/api/projects', { method: 'POST', body: JSON.stringify({ name, description }) }),
  deleteProject: (id: string) => request<void>(`/api/projects/${id}`, { method: 'DELETE' }),
  createTask: (projectId: string, title: string, description: string, config: TaskConfig) =>
    request<Task>(`/api/projects/${projectId}/tasks`, { method: 'POST', body: JSON.stringify({ title, description, config }) }),
  deleteTask: (id: string) => request<void>(`/api/tasks/${id}`, { method: 'DELETE' }),
  runTask: (taskId: string) => request<Run>(`/api/tasks/${taskId}/run`, { method: 'POST' }),
  run: (runId: string) => request<RunDetail>(`/api/runs/${runId}`),
  cancelRun: (runId: string) => request<unknown>(`/api/runs/${runId}/cancel`, { method: 'POST' }),
  memory: (projectId: string, q?: string) => request<{ embed_model: string; items: MemoryItem[] }>(`/api/projects/${projectId}/memory${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  skills: (q?: string) => request<Skill[]>(`/api/skills${q ? `?q=${encodeURIComponent(q)}` : ''}`),
  deleteSkill: (name: string) => request<void>(`/api/skills/${encodeURIComponent(name)}`, { method: 'DELETE' }),
  importSkills: (source: string, overwrite: boolean) => request<SkillImport>('/api/skills/import', { method: 'POST', body: JSON.stringify({ source, overwrite }) }),
  retest: (symbol: string, pip: number, sl: number, tp: number) => request<{ markdown?: string; active?: { level: number; level_type: string; level_interval: string; status: string; break_direction?: string; distance_pips?: number }[]; notes?: string[] | string }>(`/api/market/retest?symbol=${encodeURIComponent(symbol)}&interval=15m&level_interval=1h&pip=${pip}&sl_pips=${sl}&tp_pips=${tp}`),
  mtf: (symbol: string, pip: number, sl: number, tp: number) => request<{ markdown?: string }>(`/api/market/mtf?symbol=${encodeURIComponent(symbol)}&intervals=4h,1h,15m,5m&pip=${pip}&sl_pips=${sl}&tp_pips=${tp}&tp2_pips=${tp * 2}`),
  levels: (symbol: string) => request<{ markdown?: string }>(`/api/market/levels?symbol=${encodeURIComponent(symbol)}&intervals=4h,1h,15m`),
  backtest: (symbol: string, interval: string) => request<Backtest>(`/api/market/backtest?symbol=${encodeURIComponent(symbol)}&interval=${interval}`),
  skill: (name: string) => request<Skill>(`/api/skills/${encodeURIComponent(name)}`),
  saveSkill: (name: string, markdown: string) => request<Skill>(`/api/skills/${encodeURIComponent(name)}`, { method: 'PUT', body: JSON.stringify({ markdown }) }),
  settings: () => request<Settings>('/api/settings'),
  saveSettings: (settings: Settings) => request<Settings>('/api/settings', { method: 'PUT', body: JSON.stringify(settings) }),
  providers: () => request<{ providers: Provider[]; embed_model: string; limits?: Record<string, { status?: string; resetsAt?: number; rateLimitType?: string }> }>('/api/providers'),
}
