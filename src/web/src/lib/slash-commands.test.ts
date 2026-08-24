import { describe, expect, it } from 'vitest'
import {
  baseSlashCommands,
  filterSlashCommands,
  parsePromptInvocation,
  slashQuery,
} from './slash-commands'

describe('slash commands', () => {
  it('opens only for a slash command token at the start of the composer', () => {
    expect(slashQuery('/')).toBe('')
    expect(slashQuery('/con')).toBe('con')
    expect(slashQuery('/compact focus')).toBeNull()
    expect(slashQuery('explain /context')).toBeNull()
  })

  it('filters by command, localized description, and keywords', () => {
    expect(filterSlashCommands(baseSlashCommands, '/con').map((item) => item.value)).toEqual([
      '/context',
      '/context sources',
    ])
    expect(filterSlashCommands(baseSlashCommands, '/tokens').map((item) => item.value)).toEqual([
      '/context',
    ])
    expect(filterSlashCommands(baseSlashCommands, '/记忆').map((item) => item.value)).toEqual([
      '/memory ',
    ])
    expect(filterSlashCommands(baseSlashCommands, '/planning').map((item) => item.value)).toEqual([
      '/plan ',
    ])
  })

  it('parses quoted MCP prompt arguments without losing spaces', () => {
    expect(parsePromptInvocation(`/prompt docs:summarize topic='release notes' format=short`)).toEqual({
      reference: 'docs:summarize',
      arguments: { topic: 'release notes', format: 'short' },
    })
    expect(() => parsePromptInvocation('/prompt docs:summarize invalid')).toThrow('key=value')
  })
})
