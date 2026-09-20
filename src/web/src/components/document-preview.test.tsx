// @vitest-environment jsdom
import { Blob as NodeBlob } from 'node:buffer'
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { lumenApi } from '../lib/api/client'
import { DocumentProvider, DocumentResults, documentPath } from './document-preview'
import { MarkdownMessage } from './markdown-message'

const renderers = vi.hoisted(() => ({
  convertToHtml: vi.fn(),
  loadWorkbook: vi.fn(),
}))

vi.mock('../lib/api/client', () => ({ lumenApi: { readDocument: vi.fn() } }))
vi.mock('./model-preview', () => ({ ModelPreview: ({ buffer }: { buffer: ArrayBuffer }) => <div data-testid="model-preview">模型字节：{buffer.byteLength}</div> }))
vi.mock('mammoth', () => ({ default: {
  convertToHtml: renderers.convertToHtml,
  images: { dataUri: {} },
} }))
vi.mock('exceljs', () => ({ default: {
  Workbook: class Workbook {
    worksheets = [
      {
        name: '总览', actualRowCount: 2, actualColumnCount: 2,
        getCell: (row: number, column: number) => ({ text: [['姓名', '金额'], ['Lumen', '42']][row - 1][column - 1] }),
      },
      {
        name: '详情', actualRowCount: 1, actualColumnCount: 1,
        getCell: () => ({ text: '第二页' }),
      },
    ]
    xlsx = { load: renderers.loadWorkbook }
  },
} }))
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
  it('recognizes GLB workspace paths, preserves binary bytes and download, and aborts/revokes on close', async () => {
    const bytes = new Uint8Array([103, 108, 84, 70, 0, 255, 0, 128])
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob([bytes]))
    expect(documentPath('/project/output/model.glb', '/project')).toBe('output/model.glb')
    await render('output/model.glb')
    expect(lumenApi.readDocument).not.toHaveBeenCalled()
    await click('查看报告')
    await act(async () => { await new Promise(resolve => setTimeout(resolve, 30)) })
    expect(container.querySelector('[data-testid="model-preview"]')?.textContent).toContain('8')
    expect(container.querySelector('a[download]')?.getAttribute('href')).toBe('blob:document')
    const signal = vi.mocked(lumenApi.readDocument).mock.calls[0][1]!
    await click('关闭文档预览')
    expect(signal.aborted).toBe(true); expect(URL.revokeObjectURL).toHaveBeenCalledWith('blob:document')
  })
  it('shows the linked site favicon and falls back without breaking the link', async () => {
    await render('https://example.com/article?q=private#section')
    const link = container.querySelector('a')!
    const icon = link.querySelector('img')!
    expect(icon.getAttribute('src')).toBe('https://example.com/favicon.ico')
    expect(icon.getAttribute('referrerpolicy')).toBe('no-referrer')
    expect(link.target).toBe('_blank')
    await act(async () => { icon.dispatchEvent(new Event('error')) })
    expect(link.querySelector('img')).toBeNull()
    expect(link.querySelector('svg')).not.toBeNull()
    expect(link.href).toBe('https://example.com/article?q=private#section')
  })

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
  it('inlines local HTML styles and images while omitting unavailable or remote resources', async () => {
    vi.mocked(lumenApi.readDocument).mockImplementation(async (path) => {
      if (path === 'docs/report.html') return new Blob([
        '<link rel="stylesheet" href="styles/report.css">',
        '<main class="report"><img src="images/mark.svg"><img src="https://example.test/tracker.png"></main>',
      ])
      if (path === 'docs/styles/report.css') {
        return new Blob(['.report{color:#123;background-image:url("../images/paper.png")}'])
      }
      if (path === 'docs/images/mark.svg') return new Blob(['<svg><circle r="2"/></svg>'])
      if (path === 'docs/images/paper.png') return new Blob(['paper'])
      throw new Error(`missing: ${path}`)
    })
    await render('docs/report.html')
    await click('查看报告')
    const frame = container.querySelector('iframe')!
    expect(frame.srcdoc).toContain('.report{color:#123')
    expect(frame.srcdoc).toContain('data:image/svg+xml;base64,')
    expect(frame.srcdoc).toContain('data:image/png;base64,')
    expect(frame.srcdoc).not.toContain('https://example.test')
    expect(container.querySelector('.document-preview-note')?.textContent).toContain('3 个工作区资源已内联')
    expect(lumenApi.readDocument).toHaveBeenCalledTimes(4)
  })
  it('keeps read failures retryable and renders a sandboxed Word preview', async () => {
    vi.mocked(lumenApi.readDocument).mockRejectedValueOnce(new Error('文件已移动')).mockResolvedValueOnce(new Blob(['office']))
    renderers.convertToHtml.mockResolvedValue({ value: '<h1>季度报告</h1>', messages: [] })
    await render('report.docx')
    await click('查看报告')
    expect(container.querySelector('[role="alert"]')?.textContent).toContain('文件已移动')
    await click('重新读取')
    const frame = container.querySelector<HTMLIFrameElement>('iframe[title="Word 预览"]')!
    expect(frame).not.toBeNull()
    expect(frame.getAttribute('sandbox')).toBe('')
    expect(frame.srcdoc).toContain('季度报告')
    expect(frame.srcdoc).toContain("default-src 'none'")
    expect(container.querySelector('a[download]')).not.toBeNull()
  })
  it('renders quoted CSV cells as a scrollable table', async () => {
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob(['name,note\nLumen,"line 1,\nline 2"']))
    await render('report.csv')
    await click('查看报告')
    expect(container.querySelector('.spreadsheet-summary')?.textContent).toContain('2 行 × 2 列')
    expect(Array.from(container.querySelectorAll('td'), (cell) => cell.textContent)).toEqual([
      'name', 'note', 'Lumen', 'line 1,\nline 2',
    ])
  })
  it('renders Excel worksheets and allows switching sheets', async () => {
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob(['excel']))
    await render('report.xlsx')
    await click('查看报告')
    expect(container.querySelector('.spreadsheet-summary')?.textContent).toContain('总览 · 2 行 × 2 列')
    expect(container.querySelector('.spreadsheet-table-wrap')?.textContent).toContain('Lumen')
    await click('详情')
    expect(container.querySelector('.spreadsheet-table-wrap')?.textContent).toContain('第二页')
  })
  it('keeps the original file downloadable when Office conversion fails', async () => {
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob(['broken']))
    renderers.convertToHtml.mockRejectedValue(new Error('文档结构无效'))
    await render('broken.docx')
    await click('查看报告')
    expect(container.querySelector('.document-preview-empty[role="alert"]')?.textContent).toContain('文档结构无效')
    expect(container.querySelector('a[download="broken.docx"]')).not.toBeNull()
  })
  it('keeps PDF rendering isolated in the browser viewer', async () => {
    vi.mocked(lumenApi.readDocument).mockResolvedValue(new Blob(['%PDF']))
    await render('report.pdf')
    await click('查看报告')
    expect(container.querySelector('iframe[title="PDF 预览"]')?.getAttribute('src')).toContain('blob:document')
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
