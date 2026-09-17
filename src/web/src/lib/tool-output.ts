import type { TimelineEntry } from './api/types'

export interface WebSource {
  url: string
  title: string
  snippet: string
  published: string | null
  retrieved: boolean
  read: boolean
  linked: boolean
}

export function safeWebUrl(value: unknown): string | null {
  if (typeof value !== 'string') return null
  try {
    const url = new URL(value)
    if (!['https:', 'http:'].includes(url.protocol) || url.username || url.password) return null
    // Fragment variants identify the same retrieved page; query parameters remain significant.
    url.hash = ''
    return url.href
  } catch { return null }
}

function text(value: unknown) { return typeof value === 'string' ? value : '' }

export type ToolOutput =
  | { kind: 'search'; query: string; sources: WebSource[]; warnings: string[] }
  | { kind: 'page'; source: WebSource; content: string; hasMore: boolean }
  | { kind: 'file'; path: string; content: string; range: string; hasMore: boolean }
  | { kind: 'command'; command: string; exitCode: number; stdout: string; stderr: string; truncated: boolean }

/** Decode only known built-in contracts. Unknown/MCP output stays text, never inferred from shape. */
export function toolOutput(item: TimelineEntry): ToolOutput | null {
  if (item.kind !== 'tool') return null
  if (item.origin && item.origin !== 'builtin' && !item.origin.startsWith('builtin:')) return null
  if (item.callView?.family === 'mcp') return null
  if (item.isError || !['completed', 'ok', 'success'].includes(item.status ?? '') || typeof item.result !== 'string') return null
  let raw: Record<string, unknown>
  try {
    const parsed: unknown = JSON.parse(item.result)
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null
    raw = parsed as Record<string, unknown>
  } catch { return null }
  if (item.toolName === 'web_search' && typeof raw.query === 'string' && Array.isArray(raw.results)) {
    const sources: WebSource[] = []
    for (const value of raw.results) {
      if (!value || typeof value !== 'object' || Array.isArray(value)) return null
      const result = value as Record<string, unknown>
      const url = safeWebUrl(result.url)
      if (!url) continue
      sources.push({ url, title: text(result.title) || new URL(url).hostname, snippet: text(result.snippet),
        published: text(result.published) || null, retrieved: true, read: false, linked: false })
    }
    return { kind: 'search', query: raw.query, sources,
      warnings: Array.isArray(raw.engine_errors) ? raw.engine_errors.filter((value): value is string => typeof value === 'string') : [] }
  }
  if (item.toolName === 'web_fetch' && typeof raw.content === 'string') {
    const url = safeWebUrl(raw.url)
    if (!url) return null
    return { kind: 'page', source: { url, title: text(raw.title) || new URL(url).hostname, snippet: raw.content.slice(0, 600),
      published: text(raw.date) || null, retrieved: false, read: true, linked: false }, content: raw.content,
      hasMore: raw.has_more === true || raw.truncated === true }
  }
  if (item.toolName === 'read_file' && typeof raw.path === 'string' && typeof raw.content === 'string') {
    const range = typeof raw.start_line === 'number' && typeof raw.end_line === 'number'
      ? `第 ${raw.start_line}–${raw.end_line} 行` : ''
    return { kind: 'file', path: raw.path, content: raw.content, range, hasMore: raw.has_more === true }
  }
  if (item.toolName === 'run_command' && Array.isArray(raw.argv) && raw.argv.every((value) => typeof value === 'string')
    && typeof raw.exit_code === 'number' && typeof raw.stdout === 'string' && typeof raw.stderr === 'string') {
    return { kind: 'command', command: raw.argv.join(' '), exitCode: raw.exit_code, stdout: raw.stdout, stderr: raw.stderr,
      truncated: raw.stdout_truncated === true || raw.stderr_truncated === true }
  }
  return null
}

export function mergeSources(entries: TimelineEntry[], links: Array<{ url: string; title: string }>): WebSource[] {
  const sources = new Map<string, WebSource>()
  function add(source: WebSource) {
    const previous = sources.get(source.url)
    sources.set(source.url, previous ? { ...previous,
      title: source.read ? source.title : previous.title,
      snippet: source.read && source.snippet ? source.snippet : previous.snippet || source.snippet,
      published: source.published ?? previous.published,
      retrieved: previous.retrieved || source.retrieved, read: previous.read || source.read, linked: previous.linked || source.linked,
    } : source)
  }
  for (const entry of entries) {
    if (!['web_search', 'web_fetch'].includes(entry.toolName ?? '')) continue
    const output = toolOutput(entry)
    if (output?.kind === 'search') output.sources.forEach(add)
    if (output?.kind === 'page') add(output.source)
  }
  for (const link of links) {
    const url = safeWebUrl(link.url)
    if (url) add({ url, title: link.title || new URL(url).hostname, snippet: '', published: null,
      retrieved: false, read: false, linked: true })
  }
  return [...sources.values()]
}
