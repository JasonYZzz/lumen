import type { TimelineEntry } from './api/types'

const TAGS = ['<think>', '</think>', '<thinking>', '</thinking>']

/** Project provider text wrappers without changing journal text or retraction offsets. */
export function projectThinkingMarkup(entries: TimelineEntry[], streaming = false): TimelineEntry[] {
  let thinking = false
  let fence = ''
  let inline = ''
  let nativeThinking = false
  const projected: TimelineEntry[] = []
  for (const item of entries) {
    if (item.kind === 'user' || item.kind === 'thinking' || nativeThinking) {
      // Native reasoning is its own Markdown document, not an opening text tag.
      // Neither code fences nor wrappers may leak into another native channel/turn.
      thinking = false
      fence = ''
      inline = ''
    }
    nativeThinking = item.kind === 'thinking'
    if (!['assistant', 'commentary', 'thinking', 'progress'].includes(item.kind)) {
      projected.push(item)
      continue
    }
    const segments: TimelineEntry[] = []
    let text = ''
    let lineStart = true
    let backslashes = 0
    let kind = thinking ? 'thinking' as const : item.kind
    const flush = () => {
      if (text.trim()) segments.push({ ...item, id: `${item.id}:part:${segments.length}`, kind, text })
      text = ''
    }
    for (let index = 0; index < item.text.length;) {
      const char = item.text[index]
      if (char !== '<' && char !== '`' && char !== '~') {
        text += char
        lineStart = char === '\n' || (lineStart && /\s/.test(char))
        backslashes = char === '\\' ? backslashes + 1 : 0
        index += 1
        continue
      }
      const rest = item.text.slice(index)
      const escaped = backslashes % 2 !== 0
      backslashes = 0
      const ticks = /^(?:`+|~{3,})/.exec(rest)?.[0]
      if (ticks && !escaped) {
        if (fence) {
          if (lineStart && ticks[0] === fence[0] && ticks.length >= fence.length) fence = ''
        } else if (!inline && lineStart && ticks.length >= 3) fence = ticks
        else if (ticks[0] === '`') {
          if (!inline) inline = ticks
          else if (inline === ticks) inline = ''
        }
        text += ticks
        lineStart = false
        index += ticks.length
        continue
      }
      if (!fence && !inline && !escaped && rest[0] === '<') {
        const tag = /^<\/?(?:think|thinking)>/i.exec(rest)?.[0]
        if (tag) {
          const closing = tag.startsWith('</')
          // An orphan closing delimiter is a common
          // provider format: the text before it belongs to the reasoning block.
          flush()
          if (closing && !thinking) {
            for (const segment of segments) segment.kind = 'thinking'
          }
          thinking = !closing
          kind = thinking ? 'thinking' : item.kind
          index += tag.length
          continue
        }
        if (streaming && TAGS.some((candidate) => candidate.startsWith(rest.toLowerCase()))
          && (rest.length >= 2 || lineStart || thinking)) break
      }
      text += item.text[index]
      lineStart = false
      index += 1
    }
    flush()
    // Stable keys for ordinary text; split wrappers only when needed.
    if (segments.length === 1) segments[0].id = item.id
    projected.push(...segments)
  }
  return projected
}
