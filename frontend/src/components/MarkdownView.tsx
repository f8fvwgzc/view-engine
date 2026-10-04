import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'

/** Agent and report markdown. Raw HTML is not rendered (react-markdown default), links open in a new tab. */
export default function MarkdownView({ text, className = '' }: { text: string; className?: string }) {
  return <div className={`markdown ${className}`}>
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={{ a: ({ node: _node, ...props }) => <a {...props} target="_blank" rel="noreferrer noopener" /> }}>{text}</ReactMarkdown>
  </div>
}
