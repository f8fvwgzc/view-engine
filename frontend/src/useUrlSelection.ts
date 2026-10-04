import { useCallback, useEffect, useState } from 'react'

type Selection = { project: string | null; task: string | null; agent: string | null }

function read(): Selection {
  const params = new URLSearchParams(window.location.search)
  return { project: params.get('project'), task: params.get('task'), agent: params.get('agent') }
}

/**
 * Selection lives in the URL (`?project=…&task=…&agent=…`) so a view can be reloaded, bookmarked or shared,
 * and browser back/forward walks through projects and tasks. Agent focus uses replaceState to avoid history spam.
 */
export function useUrlSelection() {
  const [selection, setSelection] = useState<Selection>(read)

  useEffect(() => {
    const onPop = () => setSelection(read())
    window.addEventListener('popstate', onPop)
    return () => window.removeEventListener('popstate', onPop)
  }, [])

  useEffect(() => {
    const params = new URLSearchParams()
    if (selection.project) params.set('project', selection.project)
    if (selection.task) params.set('task', selection.task)
    if (selection.agent) params.set('agent', selection.agent)
    const next = `${window.location.pathname}${params.size ? `?${params}` : ''}`
    if (next === `${window.location.pathname}${window.location.search}`) return
    const current = read()
    const navigation = current.project !== selection.project || current.task !== selection.task
    window.history[navigation ? 'pushState' : 'replaceState'](null, '', next)
  }, [selection])

  const selectProject = useCallback((project: string | null) => setSelection((current) => current.project === project ? current : { project, task: null, agent: null }), [])
  const selectTask = useCallback((task: string | null) => setSelection((current) => current.task === task ? current : { ...current, task, agent: null }), [])
  const selectAgent = useCallback((agent: string | null) => setSelection((current) => current.agent === agent ? current : { ...current, agent }), [])
  return { ...selection, selectProject, selectTask, selectAgent }
}
