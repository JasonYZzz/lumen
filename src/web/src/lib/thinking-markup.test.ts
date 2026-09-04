import { describe, expect, it } from 'vitest'
import type { TimelineEntry } from './api/types'
import { projectThinkingMarkup } from './thinking-markup'
import { turnPresentation } from './turn-activity'

const entry = (text: string, kind: TimelineEntry['kind'] = 'assistant'): TimelineEntry => ({ id: 'text', kind, text })

describe('provider reasoning wrappers', () => {
  it.each(['think', 'thinking', 'THINKING'])('separates %s content from the answer in live and restored output', (tag) => {
    const input = [entry(`<${tag}>检查输入\n**分析**</${tag}>最终答案`)]
    for (const streaming of [false, true]) {
      const result = turnPresentation(input, '', streaming)
      expect(result.activity.map((item) => item.text)).toEqual(['检查输入\n**分析**'])
      expect(result.foreground.map((item) => item.text)).toEqual(['最终答案'])
    }
    expect(input[0].text).toContain(`<${tag}>`)
  })

  it('holds partial delimiters during streaming and exposes the full answer after the closing tag', () => {
    const text = '<thinking>公开过程</thinking>完成'
    for (let size = 1; size <= text.length; size++) {
      const output = projectThinkingMarkup([entry(text.slice(0, size))], true)
      expect(output.map((item) => item.text).join('')).not.toContain('<')
    }
    expect(projectThinkingMarkup([entry('a <thi')], false)[0].text).toBe('a <thi')
  })

  it('handles orphan closing delimiters and native reasoning followed by text', () => {
    expect(projectThinkingMarkup([entry('检查坐标。\n</thinking>文件已写入')]).map(({ kind, text }) => [kind, text]))
      .toEqual([['thinking', '检查坐标。\n'], ['assistant', '文件已写入']])
    expect(projectThinkingMarkup([entry('检查坐标', 'thinking'), { ...entry('</think>完成'), id: 'answer' }])
      .map((item) => item.text)).toEqual(['检查坐标', '完成'])
  })

  it('projects mixed delimiters in commentary and public progress as well as native reasoning', () => {
    for (const kind of ['commentary', 'progress'] as const) {
      const raw = entry('</think>\n<thinking>核对资料</thinking>接下来验证', kind)
      expect(projectThinkingMarkup([raw]).map(({ kind, text }) => [kind, text])).toEqual([
        ['thinking', '核对资料'], [kind, '接下来验证'],
      ])
      expect(raw.text).toContain('</think>')
    }
    expect(projectThinkingMarkup([entry('核对</thinking>继续核对', 'thinking')])
      .map(({ kind, text }) => [kind, text])).toEqual([
      ['thinking', '核对'], ['thinking', '继续核对'],
    ])
  })

  it('does not let a native reasoning code fence or an old turn hide later delimiters', () => {
    const native = entry('```xml\n未闭合的原生思考示例', 'thinking')
    const progress = { ...entry('</think><thinking>核对资料</thinking>下一步', 'commentary'), id: 'progress' }
    expect(projectThinkingMarkup([native, progress]).map(({ kind, text }) => [kind, text])).toEqual([
      ['thinking', native.text], ['thinking', '核对资料'], ['commentary', '下一步'],
    ])
    expect(projectThinkingMarkup([
      entry('<thinking>旧轮次'), { ...entry('新问题', 'user'), id: 'user' },
      { ...entry('新回答'), id: 'answer' },
    ]).at(-1)?.kind).toBe('assistant')
  })

  it('preserves literal tags in fenced code, inline code, and escaped examples', () => {
    const text = '示例 `<thinking>literal</thinking>`\n```xml\n<think>代码</think>\n```\n\\<thinking>escaped'
    expect(projectThinkingMarkup([entry(text)])).toEqual([entry(text)])
  })

  it('continues a wrapper across timeline segments and leaves tool output intact', () => {
    const output = projectThinkingMarkup([
      entry('<think>检查'), { id: 'tool', kind: 'tool', text: '<thinking>原始结果' },
      { ...entry('继续</think>结论'), id: 'next' },
    ])
    expect(output.map(({ kind, text }) => [kind, text])).toEqual([
      ['thinking', '检查'], ['tool', '<thinking>原始结果'], ['thinking', '继续'], ['assistant', '结论'],
    ])
  })
})
