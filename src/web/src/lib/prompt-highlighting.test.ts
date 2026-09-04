import { describe, expect, it } from 'vitest'
import { fileMentionAt, insertFileMention, tokenizePrompt } from './prompt-highlighting'

describe('prompt highlighting', () => {
  it('distinguishes commands, skills, and workspace references', () => {
    expect(tokenizePrompt('/plan use /skill:research with @docs/guide.md')).toEqual([
      { kind: 'command', value: '/plan' },
      { kind: 'text', value: ' use ' },
      { kind: 'skill', value: '/skill:research' },
      { kind: 'text', value: ' with ' },
      { kind: 'file', value: '@docs/guide.md' },
    ])
  })

  it('does not color slash fragments inside ordinary words', () => {
    expect(tokenizePrompt('https://example.com/a @src/app.tsx')).toEqual([
      { kind: 'text', value: 'https://example.com/a ' },
      { kind: 'file', value: '@src/app.tsx' },
    ])
  })

  it('preserves unrecognized text exactly', () => {
    expect(tokenizePrompt('普通任务')).toEqual([{ kind: 'text', value: '普通任务' }])
  })

  it('highlights quoted Chinese paths without changing a single character', () => {
    const value = '查看 @"docs/设计 文档.md" 后继续'
    const tokens = tokenizePrompt(value)
    expect(tokens.find((token) => token.kind === 'file')?.value).toBe('@"docs/设计 文档.md"')
    expect(tokens.map((token) => token.value).join('')).toBe(value)
  })
})

describe('file mention insertion', () => {
  it('replaces the token under the caret and preserves following text', () => {
    const value = '查看 @src/old.ts 然后测试'
    const mention = fileMentionAt(value, '查看 @src/'.length)!
    expect(mention).toEqual({ start: 3, end: 14, query: 'src/' })
    const result = insertFileMention(value, mention, 'docs/guide.md', false)
    expect(result.text).toBe('查看 @docs/guide.md 然后测试')
    expect(result.text.slice(0, result.caret)).toBe('查看 @docs/guide.md ')
  })

  it('quotes spaces, CJK and extensionless files for the backend parser', () => {
    for (const path of ['docs/设计.md', 'docs/my notes.md', 'README']) {
      const result = insertFileMention('@d', fileMentionAt('@d', 2)!, path, false)
      expect(result.text).toBe(`@"${path}" `)
      expect(result.caret).toBe(result.text.length)
    }
  })

  it('keeps the caret inside quoted directories for drilling into children', () => {
    const result = insertFileMention('@d', fileMentionAt('@d', 2)!, '设计', true)
    expect(result.text).toBe('@"设计/"')
    expect(fileMentionAt(result.text, result.caret)?.query).toBe('设计/')
  })

  it('does not complete email addresses, a selected range or a token away from the caret', () => {
    expect(fileMentionAt('a@example.com', 13)).toBeNull()
    expect(fileMentionAt('@src', 1, 4)).toBeNull()
    expect(fileMentionAt('text @src', 2)).toBeNull()
    expect(fileMentionAt('@"docs/file.md"', 15)).toBeNull()
  })
})
