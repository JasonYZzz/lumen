export type PromptTokenKind = 'text' | 'command' | 'skill' | 'file'

export interface PromptToken {
  kind: PromptTokenKind
  value: string
}

const TOKEN_PATTERN = /\/skill:[\w.-]+|\/[\w-]+|@"[^"\n]*"|@[\w./~-]+/g

export interface FileMention {
  start: number
  end: number
  query: string
}

/** Locate the mention at the caret, never an unrelated token at the text end. */
export function fileMentionAt(value: string, start: number, end = start): FileMention | null {
  if (start !== end) return null
  const prefix = value.slice(0, start)
  const match = prefix.match(/(?:^|[\s([{])(@(?:"[^"\n]*|[\w./~-]*))$/)
  if (!match) return null
  const token = match[1]
  const tail = value.slice(start).match(token.startsWith('@"') ? /^[^"\n]*(?:")?/ : /^[\w./~-]*/)?.[0] ?? ''
  return { start: start - token.length, end: start + tail.length, query: token.slice(1).replace(/^"/, '') }
}

export function insertFileMention(value: string, mention: FileMention, path: string, directory: boolean) {
  const target = `${path.replace(/\/$/, '')}${directory ? '/' : ''}`
  // The backend accepts quoted paths; use them for spaces, CJK and extensionless names too.
  const quote = !/^[\w./~-]+$/.test(target) || !/[./~-]/.test(target)
  const token = quote ? `@"${target}"` : `@${target}`
  const tail = value.slice(mention.end)
  const suffix = directory || /^\s/.test(tail) ? '' : ' '
  const text = `${value.slice(0, mention.start)}${token}${suffix}${tail}`
  const caret = mention.start + token.length + (directory ? (quote ? -1 : 0) : 1)
  return { text, caret }
}

export function tokenizePrompt(value: string): PromptToken[] {
  const tokens: PromptToken[] = []
  let cursor = 0

  for (const match of value.matchAll(TOKEN_PATTERN)) {
    const index = match.index
    const token = match[0]
    const boundary = index === 0 || /\s/.test(value[index - 1] ?? '')
    if (!boundary) continue
    if (index > cursor) tokens.push({ kind: 'text', value: value.slice(cursor, index) })
    tokens.push({
      kind: token.startsWith('/skill:') ? 'skill' : token.startsWith('@') ? 'file' : 'command',
      value: token,
    })
    cursor = index + token.length
  }

  if (cursor < value.length) tokens.push({ kind: 'text', value: value.slice(cursor) })
  return tokens.length > 0 ? tokens : [{ kind: 'text', value }]
}
