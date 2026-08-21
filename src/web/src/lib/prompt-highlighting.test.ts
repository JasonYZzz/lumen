import { describe, expect, it } from 'vitest'
import { tokenizePrompt } from './prompt-highlighting'

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
})
