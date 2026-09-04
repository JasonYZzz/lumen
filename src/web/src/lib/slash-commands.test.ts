import { describe, expect, it } from 'vitest'
import {
  baseSlashCommands,
  buildSlashCommands,
  filterSlashCommands,
  parsePromptInvocation,
  slashQuery,
} from './slash-commands'

describe('slash commands', () => {
  it('keeps dynamic names, descriptions, and insertion values distinct', () => {
    const commands = buildSlashCommands({
      availableModels: ['alpha', 'beta'], activeModel: 'alpha',
      skills: [{ name: 'diagram-design', description: 'Create charts and diagrams' }],
    })
    expect(commands.find((command) => command.value === '/model beta')?.label).toBe('beta')
    const skill = commands.find((command) => command.kind === 'skill')!
    expect(skill.label).toBe('diagram-design')
    expect(skill.description).toBe('Create charts and diagrams')
    expect(skill.value).toBe('/skill:diagram-design ')
    expect(filterSlashCommands(commands, '/diagram-design')).toContain(skill)
    expect(filterSlashCommands(commands, '/charts')).toContain(skill)
    expect(filterSlashCommands(commands, '/skill:diagram-design draft a diagram')).toEqual([])
  })

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

  it('filters model arguments while preserving free-form skill prompts', () => {
    const commands = [...baseSlashCommands,
      { value: '/model alpha', description: '' },
      { value: '/model beta', description: '' },
    ]
    expect(filterSlashCommands(commands, '/model ')).toHaveLength(2)
    expect(filterSlashCommands(commands, '/model BET').map((item) => item.value)).toEqual(['/model beta'])
    expect(filterSlashCommands(commands, '/skill:review some text')).toEqual([])
  })

  it('keeps model variants out of the root menu and prioritizes an exact typed name', () => {
    const commands = [...baseSlashCommands,
      { value: '/model alpha-pro', description: '' },
      { value: '/model alpha', description: '' },
      { value: '/mode auto', description: '' },
    ]
    expect(filterSlashCommands(commands, '/').some((item) => item.value === '/model alpha')).toBe(false)
    expect(filterSlashCommands(commands, '/').some((item) => item.value === '/model')).toBe(true)
    expect(filterSlashCommands(commands, '/model alpha')[0].value).toBe('/model alpha')
  })
})
