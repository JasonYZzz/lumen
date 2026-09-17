import { describe, expect, it } from 'vitest'
import type { TimelineEntry } from './api/types'
import { mergeSources, safeWebUrl, toolOutput } from './tool-output'
import { markdownStructure } from './markdown-structure'

function tool(toolName: string, result: unknown): TimelineEntry {
  return { id: toolName, kind: 'tool', text: '', toolName, status: 'completed', result: JSON.stringify(result) }
}

describe('readable outputs and source provenance', () => {
  it('merges retrieved, read and linked pages without granting evidence status to ordinary links', () => {
    const entries = [tool('web_search', { query: 'test', results: [{ url: 'https://example.com/a#one', title: '检索标题', snippet: '摘要' }] }),
      tool('web_fetch', { url: 'https://example.com/a', title: '阅读标题', content: '阅读片段', date: '2026-09-17', has_more: true })]
    const sources = mergeSources(entries, [{ url: 'https://example.com/a#two', title: '正文' }, { url: 'https://other.test/', title: '其他' }])
    expect(sources).toHaveLength(2)
    expect(sources[0]).toMatchObject({ title: '阅读标题', snippet: '阅读片段', retrieved: true, read: true, linked: true })
    expect(sources[1]).toMatchObject({ retrieved: false, read: false, linked: true })
    expect(toolOutput(entries[1])).toMatchObject({ kind: 'page', hasMore: true })
    expect(entries[0].result).toContain('#one')
  })
  it('uses declared built-in names and valid contracts, with raw fallback for unknown or failed calls', () => {
    const result = { argv: ['python', '-c', 'print(1)'], exit_code: 0, stdout: '1\n', stderr: '', stdout_truncated: true }
    expect(toolOutput(tool('run_command', result))).toMatchObject({ kind: 'command', stdout: '1\n', truncated: true })
    expect(toolOutput(tool('mcp_command', result))).toBeNull()
    expect(toolOutput({ ...tool('run_command', result), origin: 'plugin:custom' })).toBeNull()
    expect(toolOutput({ ...tool('run_command', result), isError: true })).toBeNull()
    expect(toolOutput(tool('run_command', { ...result, argv: [42] }))).toBeNull()
    expect(toolOutput(tool('read_file', { path: 'README.md', content: '1: Hello', start_line: 1, end_line: 1, has_more: true })))
      .toMatchObject({ kind: 'file', range: '第 1–1 行', hasMore: true })
  })
  it('rejects unsafe URLs while preserving significant queries', () => {
    for (const url of ['javascript:alert(1)', 'data:text/plain,test', 'https://user:secret@example.com', '/local.md']) expect(safeWebUrl(url)).toBeNull()
    expect(safeWebUrl('https://example.com?q=a#b')).toBe('https://example.com/?q=a')
  })
  it('parses Markdown structure instead of inventing references from code or images', () => {
    const structure = markdownStructure('# 标题\n## 标题\n[链接][ref]\n\n[ref]: https://example.com/a_(b)\n\n`[假链接](https://fake.test)`\n![图片](https://image.test/a.png)\n```md\n# 假目录\n[假](https://code.test)\n```')
    expect(structure.links).toEqual([{ url: 'https://example.com/a_(b)', title: '链接' }])
    expect(structure.headings.map((heading) => heading.id)).toEqual(['section-1', 'section-2'])
  })
})
