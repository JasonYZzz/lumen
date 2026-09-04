// @vitest-environment jsdom
import { Blob as NodeBlob } from 'node:buffer'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { lumenApi } from '../lib/api/client'
import { DocumentProvider, DocumentResults, documentPath } from './document-preview'
import { MarkdownMessage } from './markdown-message'

vi.mock('../lib/api/client', () => ({ lumenApi: { readDocument: vi.fn() } }))
let container: HTMLDivElement
let root: Root
beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('Blob', NodeBlob)
  URL.createObjectURL = vi.fn(() => 'blob:document')
  URL.revokeObjectURL = vi.fn()
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})
afterEach(async () => { await act(async () => root.unmount()); container.remove(); vi.unstubAllGlobals() })
async function click(label: string) {
  const button = Array.from(container.querySelectorAll('button')).find((item) => item.textContent === label || item.getAttribute('aria-label') === label)!
  expect(button).toBeDefined()
  await act(async () => { button.click() })
}
async function render(path: string) {
  await act(async () => root.render(<DocumentProvider workspace="/project"><MarkdownMessage content={`[查看报告](${path})`} /></DocumentProvider>))
}

describe('workspace document preview', () => {
  it('normalizes local links but rejects outside, hidden, encoded traversal and remote paths', () => {
    expect(documentPath('/project/docs/report.md:12', '/project')).toBe('docs/report.md')
    expect(documentPath('./docs/report.md#section', '/project')).toBe('docs/report.md')
    expect(documentPath('../appendix.md', '/project', 'docs/research')).toBe('docs/appendix.md')
    expect(documentPath('../../../outside.md', '/project', 'docs/research')).toBeNull()
    for (const path of ['/project-other/report.md', '../report.md', '%2e%2e/report.md', '.env', 'https://example.com/a.md', '//host/a.md', 'docs\\a.md']) {
      expect(documentPath(path, '/project')).toBeNull()
    }
  })
  it('reads only on click, renders Markdown, switches source, downloads, and releases its object URL', async () => {
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob(['# 测试报告\n\n内容']))
    await render('/project/docs/report.md')
    expect(lumenApi.readDocument).not.toHaveBeenCalled()
    await click('查看报告')
    expect(lumenApi.readDocument).toHaveBeenCalledWith('docs/report.md', expect.any(AbortSignal))
    expect(container.querySelector('[role="dialog"] h1')?.textContent).toBe('测试报告')
    expect(container.querySelector('a[download]')?.getAttribute('download')).toBe('report.md')
    await click('源码')
    expect(container.querySelector('.document-preview-body pre')?.textContent).toContain('# 测试报告')
    await click('关闭文档预览')
    expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:document')
  })
  it('contains HTML in a sandbox without script or network permissions', async () => {
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob(['<meta http-equiv="refresh" content="0;url=https://evil.test"><h1>Report</h1><a href="https://evil.test">External</a><script>fetch("https://evil.test")</script>']))
    await render('report.html')
    await click('查看报告')
    const frame = container.querySelector('iframe')!
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.srcdoc).toContain("default-src 'none'")
    expect(frame.srcdoc).not.toContain('http-equiv="refresh"')
    expect(frame.srcdoc).not.toContain('href="https://evil.test"')
    expect(container.querySelector('.document-preview-body script')).toBeNull()
  })
  it('keeps read failures retryable and offers download for unsupported Office preview', async () => {
    vi.mocked(lumenApi.readDocument).mockRejectedValueOnce(new Error('文件已移动')).mockResolvedValueOnce(new Blob(['office']))
    await render('report.docx')
    await click('查看报告')
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('文件已移动')
    await click('重新读取')
    expect(container.textContent).toContain('暂不支持内嵌预览')
    expect(container.querySelector('a[download]')).not.toBeNull()
  })
  it('aborts reads when closed and does not publish a late result', async () => {
    let resolve!: (blob: Blob) => void
    vi.mocked(lumenApi.readDocument).mockReturnValue(new Promise((done) => { resolve = done }))
    await render('report.md')
    await click('查看报告')
    const signal = vi.mocked(lumenApi.readDocument).mock.calls[0][1]!
    await click('关闭文档预览')
    expect(signal.aborted).toBe(true)
    await act(async () => { resolve(new Blob(['late'])) })
    expect(URL.createObjectURL).not.toHaveBeenCalled()
  })
  it('deduplicates successful file results and leaves code blocks literal', async () => {
    await act(async () => root.render(<DocumentProvider workspace="/project">
      <MarkdownMessage content={'```\ndocs/report.md\n```'} />
      <DocumentResults entries={[
        { id: 'one', kind: 'tool', text: '', toolName: 'write_file', status: 'completed', args: { path: 'docs/report.md' } },
        { id: 'two', kind: 'tool', text: '', toolName: 'edit_file', status: 'completed', args: { path: 'docs/report.md' } },
        { id: 'fail', kind: 'tool', text: '', toolName: 'write_file', status: 'error', args: { path: 'failed.md' } },
      ]} />
    </DocumentProvider>))
    expect(container.querySelectorAll('.document-card')).toHaveLength(1)
    expect(container.querySelector('pre button')).toBeNull()
    expect(container.querySelector('pre code')?.textContent).toContain('docs/report.md')
  })
})
