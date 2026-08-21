export type PromptTokenKind = 'text' | 'command' | 'skill' | 'file'

export interface PromptToken {
  kind: PromptTokenKind
  value: string
}

const TOKEN_PATTERN = /\/skill:[\w.-]+|\/[\w-]+|@[\w./~-]+/g

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
