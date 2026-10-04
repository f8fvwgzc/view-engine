import { FormEvent, useEffect, useState } from 'react'
import { Search } from 'lucide-react'
import { api } from '../api'
import type { MemoryItem } from '../types'

/** Project long-term memory: latest items, or hybrid (lexical + vector, RRF-fused) recall for a query. */
export function MemoryPanel({ projectId, refreshKey }: { projectId: string; refreshKey: number }) {
  const [query, setQuery] = useState('')
  const [submitted, setSubmitted] = useState('')
  const [items, setItems] = useState<MemoryItem[]>([])
  const [model, setModel] = useState('')
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    api.memory(projectId, submitted || undefined)
      .then((result) => { if (!cancelled) { setItems(result.items); setModel(result.embed_model) } })
      .catch(() => { if (!cancelled) setItems([]) })
      .finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [projectId, submitted, refreshKey])

  function submit(event: FormEvent) {
    event.preventDefault()
    setSubmitted(query.trim())
  }

  return <div className="memory-panel">
    <form className="memory-search" onSubmit={submit}>
      <Search size={12} />
      <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Hybrid search project memory…" aria-label="Search memory" />
    </form>
    <div className="memory-meta">{loading ? 'searching…' : `${items.length} ${submitted ? 'matches' : 'recent'} · embeddings: ${model}`}</div>
    <div className="memory-list">
      {items.length === 0 && !loading && <div className="empty">Nothing remembered yet. Findings, decisions and consolidated observations accumulate here after each run.</div>}
      {items.map((item) => <div key={item.id} className="memory-item">
        <span className={`memory-kind ${item.kind}`}>{item.kind}{item.proof_count > 1 ? ` ·${item.proof_count}` : ''}</span>
        <p>{item.content}</p>
        {item.source_url && <a href={item.source_url} target="_blank" rel="noreferrer noopener">{safeHost(item.source_url)}</a>}
      </div>)}
    </div>
  </div>
}

function safeHost(url: string) {
  try { return new URL(url).hostname } catch { return url }
}
