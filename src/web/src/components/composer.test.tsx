import React from 'react'
import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it, vi } from 'vitest'
import { Composer } from './composer'
import { buildSlashCommands } from '../lib/slash-commands'

(globalThis as typeof globalThis & { React: typeof React }).React = React

describe('Composer image capability', () => {
  it('shows just the stop action while busy with no draft, and queue controls after typing', () => {
    const props = { busy: true, queueMode: 'steer' as const, slashCommands: [], attachments: [],
      imageInputEnabled: false, onChange: vi.fn(), onQueueModeChange: vi.fn(), onAttachmentsChange: vi.fn(),
      onSubmit: vi.fn(), onStop: vi.fn() }
    const empty = renderToStaticMarkup(<Composer {...props} value="" />)
    expect(empty).toContain('aria-label="停止运行"')
    expect(empty).not.toContain('class="send-button"')
    expect(empty).not.toContain('class="queue-mode"')
    const draft = renderToStaticMarkup(<Composer {...props} value="补充内容" stopping stopError="连接中断，请重试" />)
    expect(draft).toContain('aria-label="加入运行队列"')
    expect(draft).toContain('class="queue-mode"')
    expect(draft).toMatch(/<button[^>]*class="stop-button"[^>]*disabled=""/)
    expect(draft).toContain('aria-label="正在停止"')
    expect(draft).toContain('role="alert"')
  })
  it('disables image selection when the active model is text-only', () => {
    const markup = renderToStaticMarkup(
      <Composer
        value=""
        busy={false}
        queueMode="steer"
        slashCommands={[]}
        attachments={[]}
        imageInputEnabled={false}
        onChange={vi.fn()}
        onQueueModeChange={vi.fn()}
        onAttachmentsChange={vi.fn()}
        onSubmit={vi.fn()}
        onStop={vi.fn()}
      />,
    )

    expect(markup).toMatch(/<input[^>]*type="file"[^>]*disabled=""/)
    expect(markup).toContain('aria-label="添加内容与快捷操作"')
  })
})

describe('dynamic command presentation', () => {
  it.each([
    ['diagram-design', 'Create branded architecture diagrams. '.repeat(30)],
    ['分析技能', 'First paragraph\n\nSecond paragraph with <script>literal markup</script>'],
    ['no-description', ''],
  ])('uses the Skill name as the title for %s', (name, description) => {
    const commands = buildSlashCommands({ availableModels: [], activeModel: '', skills: [{ name, description }] })
    const markup = renderToStaticMarkup(<Composer
      value="/skill:"
      busy={false}
      queueMode="steer"
      slashCommands={commands}
      attachments={[]}
      imageInputEnabled={false}
      onChange={vi.fn()}
      onQueueModeChange={vi.fn()}
      onAttachmentsChange={vi.fn()}
      onSubmit={vi.fn()}
      onCommand={vi.fn()}
      onStop={vi.fn()}
    />)
    expect(markup).toContain(`<strong>${name}</strong>`)
    expect(markup).toContain('data-kind="skill"')
    expect(markup).not.toContain('<script>')
    expect(commands.find((command) => command.kind === 'skill')?.value).toBe(`/skill:${name} `)
  })
})
