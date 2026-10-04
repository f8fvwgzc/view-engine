/** Appearance preferences: per-browser overrides of the CSS design tokens in styles.css. */
export type Appearance = { sans: string; mono: string; scale: number; accent: string; topbar: number; left: number; right: number }

export const DEFAULT_APPEARANCE: Appearance = { sans: 'Space Grotesk', mono: 'IBM Plex Mono', scale: 1, accent: '#e5ece5', topbar: 58, left: 280, right: 340 }
export const SANS_FONTS = ['Space Grotesk', 'Inter', 'system-ui'] as const
export const MONO_FONTS = ['IBM Plex Mono', 'JetBrains Mono', 'ui-monospace'] as const
export const ACCENTS = ['#e5ece5', '#9fe0ae', '#8fc7ff', '#f0a8c8', '#e6cf8f', '#c8b7ef'] as const

const KEY = 'view-engine.appearance'

export function loadAppearance(): Appearance {
  try { return { ...DEFAULT_APPEARANCE, ...JSON.parse(localStorage.getItem(KEY) ?? '{}') } } catch { return DEFAULT_APPEARANCE }
}

export function applyAppearance(value: Appearance, persist = true) {
  const root = document.documentElement.style
  const generic = (name: string, fallback: string) => name.includes('-') && !name.includes(' ') ? name : `'${name}', ${fallback}`
  root.setProperty('--sans', generic(value.sans, 'system-ui, sans-serif'))
  root.setProperty('--mono', generic(value.mono, 'ui-monospace, monospace'))
  root.setProperty('--fs', String(value.scale))
  root.setProperty('--accent', value.accent)
  root.setProperty('--topbar', `${value.topbar}px`)
  root.setProperty('--left-w', `${value.left}px`)
  root.setProperty('--right-w', `${value.right}px`)
  if (persist) { try { localStorage.setItem(KEY, JSON.stringify(value)) } catch { /* private mode: keep for this page only */ } }
}
