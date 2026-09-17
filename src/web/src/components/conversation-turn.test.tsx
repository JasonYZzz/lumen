// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { TimelineEntry } from '../lib/api/types'
import { ConversationTurn, TranscriptInspector } from './lumen-app'

let root: Root
let container: HTMLDivElement
const approve = vi.fn()
const thought: TimelineEntry = { id: 'thinking', kind: 'thinking', text: '正在核对 **公开资料**。' }
const tool: TimelineEntry = {
  id: 'tool', kind: 'tool', text: '', toolName: 'read_file', callId: 'call-1', status: 'running',
  args: { path: 'README.md' },
  callView: { family: 'read', active_verb: '正在读取', completed_verb: '已读取', detail: 'README.md' },
}
const answer: TimelineEntry = { id: 'answer', kind: 'assistant', text: '这是最终回答。' }

function summary() { return container.querySelector<HTMLButtonElement>('.turn-activity-summary')! }
function content() { return container.querySelector<HTMLDivElement>('.turn-activity-list')! }
async function render(response: TimelineEntry[], active: boolean, elapsedSeconds?: number) {
  await act(async () => root.render(<ConversationTurn
    turn={{ id: 'turn', user: { id: 'user', kind: 'user', text: '研究这个问题', elapsedSeconds }, response }}
    active={active} onApproval={approve}
  />))
}
async function click(node: HTMLElement) { await act(async () => node.click()) }

beforeEach(() => {
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  approve.mockClear()
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

describe('conversation process disclosure', () => {
  it('reads command output as text, retains raw records and warns about truncation', async () => {
    const command: TimelineEntry = { id: 'command', kind: 'tool', text: '', toolName: 'run_command', status: 'completed',
      args: { argv: ['python', 'test.py'] }, result: JSON.stringify({ argv: ['python', 'test.py'], exit_code: 0,
        stdout: 'one\ntwo\n', stderr: '', stdout_truncated: true }), callView: { family: 'command', title: '运行命令' } }
    await render([command, answer], false)
    await click(summary())
    await click(content().querySelector<HTMLButtonElement>('.tool-summary')!)
    expect(content().querySelector('.tool-output-text pre')?.textContent).toBe('one\ntwo\n')
    expect(content().querySelector('.output-limit')?.textContent).toContain('截断或分页')
    await click(content().querySelector<HTMLButtonElement>('.tool-output-toolbar button')!)
    expect(content().querySelector('.tool-readable-output')).toBeNull()
    expect(content().querySelectorAll('.tool-output-text pre')[1]?.textContent).toBe(command.result)
  })
  it('filters actual read sources separately from search results and answer-only links', async () => {
    const search: TimelineEntry = { id: 'search', kind: 'tool', text: '', toolName: 'web_search', status: 'completed',
      result: JSON.stringify({ query: '资料', results: [{ url: 'https://example.com/', title: '已检索页面' }] }) }
    const fetch: TimelineEntry = { id: 'fetch', kind: 'tool', text: '', toolName: 'web_fetch', status: 'completed',
      result: JSON.stringify({ url: 'https://read.test/', title: '已阅读页面', content: '真实阅读片段' }) }
    await render([search, fetch, { ...answer, text: '[正文链接](https://linked.test/)' }], false)
    const trigger = container.querySelector<HTMLButtonElement>('.turn-sources-summary button')!
    trigger.focus()
    await click(trigger)
    expect(document.querySelectorAll('.sources-list li')).toHaveLength(3)
    await click(Array.from(document.querySelectorAll<HTMLButtonElement>('.sources-filters button')).find((button) => button.textContent === '已阅读片段')!)
    expect(document.querySelectorAll('.sources-list li')).toHaveLength(1)
    expect(document.querySelector('.sources-list')?.textContent).toContain('真实阅读片段')
    await click(document.querySelector<HTMLButtonElement>('[aria-label="关闭来源面板"]')!)
    expect(document.querySelector('.sources-panel')).toBeNull()
    expect(document.activeElement).toBe(trigger)
  })
  it('preserves manual opening at completion and highlights all concurrent tools', async () => {
    await render([tool, { ...tool, id: 'other', callId: 'call-2' }], true)
    expect(content().querySelectorAll('.web-tool-card.is-live')).toHaveLength(2)
    await click(summary())
    await click(summary())
    await render([{ ...tool, status: 'completed' }, answer], false)
    expect(content().hidden).toBe(false)
  })
  it('shows an immediate live placeholder before the first response event arrives', async () => {
    await render([], true)
    expect(container.querySelector('.assistant-turn')).not.toBeNull()
    expect(container.querySelector('.turn-live-placeholder[role="status"]')?.textContent).toBe('正在思考')
    expect(container.querySelector('.turn-live-placeholder img.thinking-orb')?.getAttribute('src'))
      .toBe('/lumen-claude-amber.svg')
    expect(container.querySelectorAll('.turn-live-placeholder img.thinking-orb')).toHaveLength(1)
    expect(summary()).toBeNull()
  })

  it('uses the same readable projection for transcript display and copy, retaining raw diagnostics', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    vi.stubGlobal('navigator', { clipboard: { writeText } })
    const timeline: TimelineEntry[] = [
      { id: 'reason', kind: 'thinking', text: '核对资料</thinking>' },
      { id: 'progress', kind: 'commentary', text: '</think>\n<thinking>检查结果</thinking>继续验证' },
      { id: 'tool', kind: 'tool', text: '<think>工具原始内容</think>' },
    ]
    const original = JSON.stringify(timeline)
    const show = async (density: 'normal' | 'verbose') => act(async () => root.render(
      <TranscriptInspector timeline={timeline} density={density} onClose={() => {}} onToggleDensity={() => {}} />,
    ))
    await show('normal')
    const notes = container.querySelectorAll('.transcript-record:not(.is-tool)')
    expect(Array.from(notes, (node) => node.textContent).join('')).not.toMatch(/<\/?think(?:ing)?>/)
    expect(container.querySelector('.is-tool')?.textContent).toContain('<think>工具原始内容</think>')
    await click(container.querySelector<HTMLButtonElement>('[aria-label="复制当前记录"]')!)
    expect(writeText).toHaveBeenLastCalledWith(
      '[思考] 核对资料\n\n[思考] 检查结果\n\n[进展] 继续验证\n\n[工具] <think>工具原始内容</think>',
    )
    await show('verbose')
    expect(container.querySelector('.is-commentary')?.textContent).toContain('</think>')
    await click(container.querySelector<HTMLButtonElement>('[aria-label="复制当前记录"]')!)
    expect(writeText.mock.lastCall?.[0]).toContain('</think>')
    expect(JSON.stringify(timeline)).toBe(original)
  })

  it('keeps plan records out of both active and completed conversation history', async () => {
    const plan: TimelineEntry = { id: 'plan', kind: 'plan', text: '', plan: {
      goal: '完整任务目标', revision: 3, state_version: 3, lifecycle: 'executing', approved_revision: 3,
      steps: [{ id: 'one', title: '检查来源', status: 'in_progress', note: '核对官方资料', depends_on: [], acceptance_criteria: [] }],
    } }
    await render([thought, plan], true)
    expect(content().querySelector('.plan-summary')).toBeNull()
    expect(content().querySelector('.plan-goal')).toBeNull()
    expect(content().querySelector('progress')).toBeNull()
    await render([thought, { ...plan, plan: { ...plan.plan!, steps: [{ ...plan.plan!.steps[0], status: 'completed' }] } }, answer], false)
    expect(content().hidden).toBe(true)
    expect(container.querySelector('.assistant-turn-body > .web-plan')).toBeNull()
    expect(container.querySelector('.timeline-assistant')?.textContent).toContain(answer.text)
    await click(summary())
    expect(content().textContent).not.toContain('检查来源')
  })
  it('shows streamed process in order, folds only after completion, and preserves manual reopening', async () => {
    await render([thought, tool], true)
    expect(content().hidden).toBe(false)
    expect(summary().getAttribute('aria-controls')).toBe(content().id)
    expect(content().querySelector<HTMLDetailsElement>('.process-diagnostics')?.open).toBe(false)
    expect(content().querySelector('.process-diagnostics .markdown-body strong')?.textContent).toBe('公开资料')
    expect(content().querySelector('.tool-glyph svg')).not.toBeNull()
    expect(content().querySelector('.web-tool-card.is-live')).not.toBeNull()
    expect(container.querySelector('.turn-live-placeholder')).toBeNull()
    expect(summary().textContent).toContain('正在阅读文件')
    expect(summary().querySelector('img.thinking-orb')?.getAttribute('src'))
      .toBe('/lumen-claude-amber.svg')
    expect(content().querySelector('.tool-title-line')?.textContent).toBe('正在读取 · README.md')
    // Receiving the final answer does not collapse while the run is still active.
    await render([thought, { ...tool, status: 'completed' }, answer], true)
    expect(content().hidden).toBe(false)
    expect(content().querySelector('.web-tool-card.is-live')).toBeNull()
    expect(container.querySelector('.assistant-turn-body > .turn-live-placeholder')).toBeNull()
    expect(content().querySelector('.timeline-assistant')).toBeNull()
    expect(container.querySelector('.timeline-assistant')?.textContent).toContain('这是最终回答')
    await render([thought, { ...tool, status: 'completed' }, answer], false, 155.9)
    expect(content().hidden).toBe(true)
    expect(summary().textContent).toContain('2m 35s')
    await click(summary())
    expect(content().hidden).toBe(false)
    await render([thought, { ...tool, status: 'completed' }, answer], false, 155.9)
    expect(content().hidden).toBe(false)
    expect(content().querySelector('.tool-title-line')?.textContent).toBe('已读取 · README.md')
  })

  it('collapses all intermediate assistant narration with the process after completion', async () => {
    const first = { id: 'first', kind: 'assistant' as const, text: '先搜索相关天气信息。' }
    const second = { id: 'second', kind: 'assistant' as const, text: '再打开权威页面核实。' }
    const final = { id: 'final', kind: 'assistant' as const, text: '这是最终天气总结。' }
    await render([
      thought,
      first,
      { ...tool, status: 'completed' },
      second,
      { id: 'last-thought', kind: 'thinking', text: '整理答案' },
      final,
    ], false, 27)

    expect(content().hidden).toBe(true)
    expect(container.querySelectorAll('.assistant-turn-body > .timeline-assistant')).toHaveLength(1)
    expect(container.querySelector('.assistant-turn-body > .timeline-assistant')?.textContent)
      .toContain(final.text)
    expect(container.querySelector('.assistant-turn-body > .timeline-assistant')?.textContent)
      .not.toContain(first.text)
    expect(container.querySelector('.assistant-turn-body > .timeline-assistant')?.textContent)
      .not.toContain(second.text)

    await click(summary())
    expect(content().textContent).toContain(first.text)
    expect(content().textContent).toContain(second.text)
    expect(content().textContent).not.toContain(final.text)
  })

  it('respects manual collapse during streaming and opens when a new approval needs attention', async () => {
    await render([thought, tool], true)
    await click(summary())
    await render([{ ...thought, text: '更多进展' }, tool], true)
    expect(content().hidden).toBe(true)
    const pending = { ...tool, pendingApproval: true, status: 'pending', presentation: { title: '读取文件', preview: '预览', full_text: '完整审批说明' } }
    await render([thought, pending], true)
    expect(content().hidden).toBe(false)
    await click(summary())
    expect(content().hidden).toBe(true)
    const allow = Array.from(container.querySelectorAll<HTMLButtonElement>('.turn-attention button')).find((node) => node.textContent === '允许一次')!
    await click(allow)
    expect(approve).toHaveBeenCalledWith('call-1', true, 'once')
    await render([thought, { ...pending, pendingApproval: false, status: 'completed', result: '实际结果' }], true)
    expect(content().querySelector('.approval-request')).toBeNull()
    expect(container.querySelector('.turn-attention')).toBeNull()
    await click(summary())
    await click(content().querySelector<HTMLButtonElement>('.tool-summary')!)
    expect(content().querySelector('.tool-detail')?.textContent).toContain('实际结果')
    expect(content().querySelector('.approval-request')?.textContent).toContain('完整审批说明')
  })

  it.each(['failed', 'cancelled', 'interrupted'])('keeps %s details visible on termination and restore', async (status) => {
    await render([thought, tool], true)
    await click(summary())
    const terminal: TimelineEntry = { id: 'terminal', kind: 'system', text: '需要处理的信息', status }
    await render([thought, tool, terminal], false)
    expect(content().hidden).toBe(false)
    expect(content().textContent).not.toContain('需要处理的信息')
    expect(container.textContent).toContain('需要处理的信息')
    expect(summary().textContent).not.toContain('已完成处理')
  })

  it('collapses retry narration at clarification and presents actionable choices once', async () => {
    const onAnswer = vi.fn().mockResolvedValue(undefined)
    const question = { id: 'q1', question: '要安装哪个 Skill？', choices: ['前端设计', '排版优化'], created_at: '' }
    const response: TimelineEntry[] = [
      { id: 'debug', kind: 'assistant', text: 'The choices parameter got serialized as a string.' },
      { id: 'question', kind: 'system', status: 'waiting_for_user', text: question.question },
    ]
    await act(async () => root.render(<ConversationTurn
      turn={{ id: 'turn', user: { id: 'user', kind: 'user', text: '安装 Skill' }, response }}
      active={false} onApproval={approve} clarification={question} onAnswer={onAnswer}
    />))
    expect(content().hidden).toBe(true)
    expect(container.querySelector('.timeline-assistant')).toBeNull()
    expect(container.querySelector('.clarification-prompt h3')?.textContent).toBe(question.question)
    expect(container.querySelectorAll('.timeline-note.is-system')).toHaveLength(0)
    await click(container.querySelector<HTMLButtonElement>('.clarification-options button')!)
    expect(onAnswer).toHaveBeenCalledExactlyOnceWith('前端设计')
    await click(summary())
    expect(content().textContent).toContain('serialized as a string')
  })

  it('retains the clarification after a failed answer and prevents duplicate submissions', async () => {
    let reject: (error: Error) => void = () => {}
    const onAnswer = vi.fn(() => new Promise<void>((_resolve, fail) => { reject = fail }))
    await act(async () => root.render(<ConversationTurn
      turn={{ id: 'turn', user: null, response: [{ id: 'q', kind: 'system', text: '请选择', status: 'waiting_for_user' }] }}
      active={false} onApproval={approve} onAnswer={onAnswer}
      clarification={{ id: 'q', question: '请选择', choices: ['全局安装'], created_at: '' }}
    />))
    const option = container.querySelector<HTMLButtonElement>('.clarification-options button')!
    await act(async () => { option.click(); option.click() })
    expect(onAnswer).toHaveBeenCalledTimes(1)
    expect(option.disabled).toBe(true)
    await act(async () => reject(new Error('连接中断，请重试')))
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('连接中断')
    expect(option.disabled).toBe(false)
  })

  it('returns keyboard focus to the disclosure when completion hides focused tool details', async () => {
    await render([thought, tool], true)
    const button = content().querySelector<HTMLButtonElement>('.tool-summary')!
    button.focus()
    await render([thought, tool, answer], false, 60)
    expect(document.activeElement).toBe(summary())
    expect(content().hidden).toBe(true)
  })

  it('does not create an empty process for plain answers or invent a duration for older records', async () => {
    await render([answer], false)
    expect(summary()).toBeNull()
    await render([thought, answer], false)
    expect(content().hidden).toBe(true)
    expect(summary().textContent).toBe('已完成处理')
  })
})
