import { lazy, Suspense } from 'react'

// react-markdown + remark-gfm are only needed once a report or agent output is opened: load them on demand.
const MarkdownView = lazy(() => import('./MarkdownView'))

export function Markdown(props: { text: string; className?: string }) {
  return <Suspense fallback={<div className={`markdown ${props.className ?? ''}`}><p>{props.text.slice(0, 400)}</p></div>}><MarkdownView {...props} /></Suspense>
}
