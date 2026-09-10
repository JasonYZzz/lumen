'use client'

import { ArrowDown, ArrowClockwise, CircleNotch, FileText, X } from '@phosphor-icons/react'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { lumenApi } from '../lib/api/client'
import type { TimelineEntry } from '../lib/api/types'
import { MarkdownMessage } from './markdown-message'
import { useModalFocus } from './use-modal-focus'

const extensions = /\.(md|markdown|txt|html?|json|csv|tsv|log|py|tsx?|jsx?|css|ya?ml|pdf|png|jpe?g|webp|gif|svg|docx|xlsx|pptx)$/i
const MAX_TABLE_ROWS = 500
const MAX_TABLE_COLUMNS = 50
const MAX_WORKSHEETS = 20

interface TableSheet {
  name: string
  rows: string[][]
  totalRows: number
  totalColumns: number
}

interface PreviewResult {
  text: string | null
  url: string
  htmlPreview?: string
  inlinedResources?: number
  omittedResources?: number
  wordHtml?: string
  sheets?: TableSheet[]
  warning?: string
  previewError?: string
}

const previewResourceExtensions = /\.(css|png|jpe?g|webp|gif|svg|avif|ico|woff2?|ttf|otf)$/i
const HTML_PREVIEW_RESOURCE_LIMIT = 64
const HTML_PREVIEW_BYTE_LIMIT = 16 * 1024 * 1024

function previewResourcePath(value: string, base: string): string | null {
  if (/^[a-z][a-z\d+.-]*:/i.test(value) || value.startsWith('//')) return null
  let path: string
  try { path = decodeURIComponent(value.split(/[?#]/)[0]) } catch { return null }
  const fromRoot = path.startsWith('/')
  path = path.replace(/^\/+|^\.\//g, '')
  if (!path || path.includes('\\') || !previewResourceExtensions.test(path)) return null
  const parts: string[] = []
  for (const part of (fromRoot || !base ? path : `${base}/${path}`).split('/')) {
    if (part === '.') continue
    if (part === '..') {
      if (!parts.length) return null
      parts.pop()
      continue
    }
    if (!part || part.startsWith('.')) return null
    parts.push(part)
  }
  return parts.join('/')
}

function previewResourceType(path: string) {
  const extension = path.split('.').at(-1)?.toLowerCase()
  return ({
    css: 'text/css', png: 'image/png', jpg: 'image/jpeg', jpeg: 'image/jpeg', webp: 'image/webp',
    gif: 'image/gif', svg: 'image/svg+xml', avif: 'image/avif', ico: 'image/x-icon',
    woff: 'font/woff', woff2: 'font/woff2', ttf: 'font/ttf', otf: 'font/otf',
  } as Record<string, string>)[extension ?? ''] ?? 'application/octet-stream'
}

async function blobDataUrl(blob: Blob, path: string) {
  const bytes = new Uint8Array(await blob.arrayBuffer())
  let binary = ''
  for (let offset = 0; offset < bytes.length; offset += 0x8000) {
    binary += String.fromCharCode(...bytes.subarray(offset, offset + 0x8000))
  }
  return `data:${previewResourceType(path)};base64,${btoa(binary)}`
}

async function replaceCssResources(
  css: string,
  base: string,
  load: (path: string) => Promise<Blob | null>,
) {
  const source = css.replace(/@import\s+(?:url\()?\s*["']?[^;"')]+["']?\s*\)?\s*;/gi, '')
  const expression = /url\(\s*(?:(["'])(.*?)\1|([^)'"\s][^)]*?))\s*\)/gi
  const matches = Array.from(source.matchAll(expression))
  if (!matches.length) return source
  const replacements = await Promise.all(matches.map(async (match) => {
    const value = (match[2] ?? match[3] ?? '').trim()
    if (!value || value.startsWith('data:') || value.startsWith('#')) return match[0]
    const path = previewResourcePath(value, base)
    if (!path) return 'url("")'
    const blob = await load(path)
    return blob ? `url("${await blobDataUrl(blob, path)}")` : 'url("")'
  }))
  let rewritten = ''
  let cursor = 0
  matches.forEach((match, index) => {
    const start = match.index ?? cursor
    rewritten += source.slice(cursor, start) + replacements[index]
    cursor = start + match[0].length
  })
  return rewritten + source.slice(cursor)
}

async function staticHtmlPreview(text: string, path: string, signal: AbortSignal) {
  const document = new DOMParser().parseFromString(text, 'text/html')
  const base = path.split('/').slice(0, -1).join('/')
  const cache = new Map<string, Promise<Blob | null>>()
  let loadedBytes = 0
  let loadedResources = 0
  let omittedResources = 0
  const load = (resourcePath: string): Promise<Blob | null> => {
    const cached = cache.get(resourcePath)
    if (cached) return cached
    const request = (async () => {
      if (cache.size >= HTML_PREVIEW_RESOURCE_LIMIT) {
        omittedResources += 1
        return null
      }
      try {
        const blob = await lumenApi.readDocument(resourcePath, signal)
        if (signal.aborted) return null
        if (loadedBytes + blob.size > HTML_PREVIEW_BYTE_LIMIT) {
          omittedResources += 1
          return null
        }
        loadedBytes += blob.size
        loadedResources += 1
        return blob
      } catch (cause) {
        if (signal.aborted) throw cause
        omittedResources += 1
        return null
      }
    })()
    cache.set(resourcePath, request)
    return request
  }

  document.querySelectorAll('base, meta[http-equiv], script, iframe, object, embed').forEach((node) => node.remove())
  document.querySelectorAll('a[href], area[href]').forEach((node) => {
    if (!node.getAttribute('href')?.startsWith('#')) node.removeAttribute('href')
  })
  document.querySelectorAll('form[action]').forEach((node) => node.removeAttribute('action'))
  document.querySelectorAll('[srcset]').forEach((node) => node.removeAttribute('srcset'))

  const stylesheets = Array.from(document.querySelectorAll<HTMLLinkElement>('link[rel~="stylesheet"][href]'))
  await Promise.all(stylesheets.map(async (link) => {
    const stylesheetPath = previewResourcePath(link.getAttribute('href') ?? '', base)
    const blob = stylesheetPath ? await load(stylesheetPath) : null
    if (!blob || !stylesheetPath) {
      link.remove()
      return
    }
    const style = document.createElement('style')
    style.textContent = await replaceCssResources(
      await blob.text(), stylesheetPath.split('/').slice(0, -1).join('/'), load,
    )
    link.replaceWith(style)
  }))
  document.querySelectorAll('link').forEach((node) => node.remove())

  const imageAttributes: Array<[Element, string]> = [
    ...Array.from(document.querySelectorAll('img[src]'), (node): [Element, string] => [node, 'src']),
    ...Array.from(document.querySelectorAll('image[href]'), (node): [Element, string] => [node, 'href']),
  ]
  await Promise.all(imageAttributes.map(async ([node, attribute]) => {
    const value = node.getAttribute(attribute) ?? ''
    if (value.startsWith('data:')) return
    const resourcePath = previewResourcePath(value, base)
    const blob = resourcePath ? await load(resourcePath) : null
    if (!blob || !resourcePath) node.removeAttribute(attribute)
    else node.setAttribute(attribute, await blobDataUrl(blob, resourcePath))
  }))

  await Promise.all(Array.from(document.querySelectorAll('style'), async (style) => {
    style.textContent = await replaceCssResources(style.textContent ?? '', base, load)
  }))
  await Promise.all(Array.from(document.querySelectorAll<HTMLElement>('[style]'), async (node) => {
    node.setAttribute('style', await replaceCssResources(node.getAttribute('style') ?? '', base, load))
  }))

  const policy = document.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; "
    + "media-src data:; form-action 'none'; base-uri 'none'"
  document.head.prepend(policy)
  return { html: document.documentElement.outerHTML, loadedResources, omittedResources }
}

export function documentPath(value: string, workspace: string, base = ''): string | null {
  if (/^[a-z][a-z\d+.-]*:/i.test(value) || value.startsWith('//')) return null
  let path: string
  try { path = decodeURIComponent(value.split('#')[0]).replace(/:\d+(?::\d+)?$/, '') } catch { return null }
  const root = workspace.replace(/\/$/, '')
  const absolute = path.startsWith('/')
  if (root && path.startsWith(`${root}/`)) path = path.slice(root.length + 1)
  path = path.replace(/^\.\//, '')
  if (path.startsWith('/') || path.includes('\\') || !extensions.test(path)) return null
  const parts: string[] = []
  for (const part of (!absolute && base ? `${base}/${path}` : path).split('/')) {
    if (part === '.') continue
    if (part === '..') { if (!parts.length) return null; parts.pop(); continue }
    if (!part || part.startsWith('.')) return null
    parts.push(part)
  }
  return parts.join('/')
}

const Documents = createContext<{ workspace: string; base?: string; open: (path: string) => void } | null>(null)

function staticHtml(text: string, style = ''): string {
  const document = new DOMParser().parseFromString(text, 'text/html')
  document.querySelectorAll('base, meta[http-equiv]').forEach((node) => node.remove())
  document.querySelectorAll('a[href], area[href]').forEach((node) => {
    if (!node.getAttribute('href')?.startsWith('#')) node.removeAttribute('href')
  })
  const policy = document.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; form-action 'none'; base-uri 'none'"
  document.head.prepend(policy)
  if (style) {
    const styles = document.createElement('style')
    styles.textContent = style
    document.head.append(styles)
  }
  return document.documentElement.outerHTML
}

const wordPreviewStyle = `
  :root { color-scheme: light; font-family: Arial, sans-serif; color: #242424; background: #f3f3f1; }
  body { box-sizing: border-box; width: min(820px, calc(100% - 32px)); min-height: calc(100vh - 32px);
    margin: 16px auto; padding: clamp(28px, 6vw, 72px); background: white; box-shadow: 0 2px 14px rgb(0 0 0 / 10%);
    font-size: 15px; line-height: 1.65; overflow-wrap: anywhere; }
  img { max-width: 100%; height: auto; } table { width: 100%; border-collapse: collapse; }
  th, td { border: 1px solid #d8d8d4; padding: 6px 8px; text-align: left; }
  h1, h2, h3 { line-height: 1.3; } a { color: inherit; text-decoration: none; }
`

export function parseDelimitedTable(text: string, delimiter = ','): TableSheet {
  const rows: string[][] = []
  let row: string[] = []
  let cell = ''
  let quoted = false
  const value = text.replace(/^\uFEFF/, '')
  for (let index = 0; index < value.length; index += 1) {
    const character = value[index]
    if (character === '"') {
      if (quoted && value[index + 1] === '"') {
        cell += '"'
        index += 1
      } else {
        quoted = !quoted
      }
    } else if (character === delimiter && !quoted) {
      row.push(cell)
      cell = ''
    } else if ((character === '\n' || character === '\r') && !quoted) {
      row.push(cell)
      rows.push(row)
      row = []
      cell = ''
      if (character === '\r' && value[index + 1] === '\n') index += 1
    } else {
      cell += character
    }
  }
  if (cell || row.length) {
    row.push(cell)
    rows.push(row)
  }
  const totalColumns = rows.reduce((largest, current) => Math.max(largest, current.length), 0)
  return {
    name: '数据',
    rows: rows.slice(0, MAX_TABLE_ROWS).map((current) => current.slice(0, MAX_TABLE_COLUMNS)),
    totalRows: rows.length,
    totalColumns,
  }
}

async function wordPreview(buffer: ArrayBuffer) {
  const mammoth = (await import('mammoth')).default
  const converted = await mammoth.convertToHtml(
    { arrayBuffer: buffer },
    { convertImage: mammoth.images.dataUri, externalFileAccess: false },
  )
  return {
    html: staticHtml(converted.value, wordPreviewStyle),
    warning: converted.messages.length ? `转换时有 ${converted.messages.length} 项格式提示，预览可能与原文略有差异。` : undefined,
  }
}

async function excelPreview(buffer: ArrayBuffer): Promise<{ sheets: TableSheet[]; warning?: string }> {
  const ExcelJS = (await import('exceljs')).default
  const workbook = new ExcelJS.Workbook()
  await workbook.xlsx.load(buffer)
  const sheets = workbook.worksheets.slice(0, MAX_WORKSHEETS).map((worksheet) => {
    const totalRows = worksheet.actualRowCount
    const totalColumns = worksheet.actualColumnCount
    const rows = Array.from({ length: Math.min(totalRows, MAX_TABLE_ROWS) }, (_, rowIndex) => (
      Array.from({ length: Math.min(totalColumns, MAX_TABLE_COLUMNS) }, (_, columnIndex) => (
        worksheet.getCell(rowIndex + 1, columnIndex + 1).text
      ))
    ))
    return { name: worksheet.name, rows, totalRows, totalColumns }
  })
  return {
    sheets,
    warning: workbook.worksheets.length > MAX_WORKSHEETS
      ? `工作簿包含 ${workbook.worksheets.length} 个工作表，预览显示前 ${MAX_WORKSHEETS} 个。`
      : undefined,
  }
}

export function DocumentLink({ href, children }: { href: string; children: ReactNode }) {
  const context = useContext(Documents)
  const path = context && documentPath(href, context.workspace, context.base)
  return path ? <button type="button" className="document-link" onClick={() => context!.open(path)}><FileText size={15} aria-hidden="true" />{children}</button>
    : <a href={href}>{children}</a>
}

export function DocumentCode({ children, className }: { children?: ReactNode; className?: string }) {
  const context = useContext(Documents)
  const path = !className && typeof children === 'string' && context && documentPath(children, context.workspace, context.base)
  return path ? <button type="button" className="document-link" onClick={() => context!.open(path)}><FileText size={15} aria-hidden="true" />{children}</button>
    : <code className={className}>{children}</code>
}

export function DocumentResults({ entries }: { entries: TimelineEntry[] }) {
  const context = useContext(Documents)
  if (!context) return null
  const paths = [...new Set(entries.flatMap((entry) => {
    if (entry.kind !== 'tool' || entry.isError || !['completed', 'ok', 'success'].includes(entry.status ?? '')
      || !['write_file', 'edit_file'].includes(entry.toolName ?? '') || typeof entry.args?.path !== 'string') return []
    const path = documentPath(entry.args.path, context.workspace)
    return path ? [path] : []
  }))]
  return paths.length ? <div className="document-results" aria-label="本轮文件">
    {paths.map((path) => <button type="button" className="document-card" key={path} onClick={() => context.open(path)}>
      <FileText size={24} aria-hidden="true" /><span><strong>{path.split('/').at(-1)}</strong><small>{path}</small></span><span>预览</span>
    </button>)}
  </div> : null
}

export function DocumentProvider({ workspace, children }: { workspace: string; children: ReactNode }) {
  const [path, setPath] = useState<string | null>(null)
  return <Documents.Provider value={{ workspace, open: setPath }}>{children}
    {path && <DocumentPreview key={path} path={path} onClose={() => setPath(null)} />}
  </Documents.Provider>
}

function DocumentPreview({ path, onClose }: { path: string; onClose: () => void }) {
  const context = useContext(Documents)
  const ref = useModalFocus(onClose)
  const [result, setResult] = useState<PreviewResult | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [source, setSource] = useState(false)
  const [activeSheet, setActiveSheet] = useState(0)
  const extension = path.split('.').at(-1)!.toLowerCase()
  const markdown = ['md', 'markdown'].includes(extension)
  const html = ['html', 'htm'].includes(extension)
  const image = ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(extension)
  const pdf = extension === 'pdf'
  const word = extension === 'docx'
  const excel = extension === 'xlsx'
  const delimited = ['csv', 'tsv'].includes(extension)
  const binary = word || excel || extension === 'pptx'
  useEffect(() => {
    const controller = new AbortController()
    let url: string | undefined
    setError('')
    setResult(null)
    void lumenApi.readDocument(path, controller.signal).then(async (blob) => {
      const text = !image && !pdf && !binary && blob.size <= 1024 * 1024 ? await blob.text() : null
      if (controller.signal.aborted) return
      url = URL.createObjectURL(new Blob([blob], { type: pdf ? 'application/pdf' : image ? `image/${extension === 'jpg' ? 'jpeg' : extension}` : 'application/octet-stream' }))
      if (word) {
        try {
          const preview = await wordPreview(await blob.arrayBuffer())
          if (!controller.signal.aborted) setResult({ text: null, url, wordHtml: preview.html, warning: preview.warning })
        } catch (cause) {
          if (!controller.signal.aborted) setResult({
            text: null,
            url,
            previewError: cause instanceof Error ? cause.message : 'Word 文档转换失败',
          })
        }
      } else if (excel) {
        try {
          const preview = await excelPreview(await blob.arrayBuffer())
          if (!controller.signal.aborted) setResult({ text: null, url, sheets: preview.sheets, warning: preview.warning })
        } catch (cause) {
          if (!controller.signal.aborted) setResult({
            text: null,
            url,
            previewError: cause instanceof Error ? cause.message : 'Excel 工作簿解析失败',
          })
        }
      } else if (delimited && text !== null) {
        setResult({ text, url, sheets: [parseDelimitedTable(text, extension === 'tsv' ? '\t' : ',')] })
      } else if (html && text !== null) {
        try {
          const preview = await staticHtmlPreview(text, path, controller.signal)
          if (!controller.signal.aborted) setResult({
            text,
            url,
            htmlPreview: preview.html,
            inlinedResources: preview.loadedResources,
            omittedResources: preview.omittedResources,
          })
        } catch (cause) {
          if (!controller.signal.aborted) setResult({
            text,
            url,
            htmlPreview: staticHtml(text),
            warning: cause instanceof Error ? cause.message : '工作区资源解析失败',
          })
        }
      } else {
        setResult({ text, url })
      }
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '读取失败') })
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [attempt, path, image, pdf, binary, word, excel, delimited, html, extension])
  return <div className="document-preview-backdrop" onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={ref} className="document-preview" role="dialog" aria-modal="true" aria-label={`文档预览：${path}`} tabIndex={-1}>
      <header><div><strong>{path.split('/').at(-1)}</strong><small>{path} · 当前文件</small></div>
        {result && result.text !== null && (markdown || html) && <button type="button" onClick={() => setSource(!source)}>{source ? '预览' : '源码'}</button>}
        {result && <a href={result.url} download={path.split('/').at(-1)}><ArrowDown size={16} />下载</a>}
        <button type="button" aria-label="关闭文档预览" onClick={onClose}><X size={20} /></button></header>
      <div className="document-preview-body">
        {error ? <div role="alert"><p>{error}</p><button type="button" onClick={() => setAttempt(attempt + 1)}><ArrowClockwise size={16} />重新读取</button></div>
          : !result ? <div className="document-preview-loading" role="status">
              <CircleNotch size={20} className="spin" aria-hidden="true" />
              <span><strong>正在准备预览</strong><small>读取文件并安全处理工作区资源…</small></span>
            </div>
          : pdf ? <iframe title="PDF 预览" src={result.url} />
          : image ? <img src={result.url} alt={path.split('/').at(-1)} />
          : result.previewError ? <div className="document-preview-empty" role="alert">
              <strong>无法生成此文件的预览</strong><span>{result.previewError}</span><span>仍可下载原文件查看。</span>
            </div>
          : result.wordHtml ? <><p className="document-preview-note">Word 静态预览 · 复杂分页和字体可能与桌面版不同。</p>
              {result.warning && <p className="document-preview-note">{result.warning}</p>}
              <iframe title="Word 预览" sandbox="" srcDoc={result.wordHtml} /></>
          : result.sheets ? <SpreadsheetPreview sheets={result.sheets} active={activeSheet} onChange={setActiveSheet} warning={result.warning} />
          : result.text === null ? <p>此格式或大小暂不支持内嵌预览，请下载后打开。</p>
          : source ? <pre>{result.text}</pre>
          : html ? <><p className="document-preview-note">
              <span>静态安全预览 · 脚本和网络访问已禁用</span>
              <span>{result.inlinedResources ?? 0} 个工作区资源已内联
                {result.omittedResources ? ` · ${result.omittedResources} 个资源不可用` : ''}</span>
              {result.warning && <span>{result.warning}</span>}
            </p><iframe title="HTML 静态预览" sandbox="" srcDoc={result.htmlPreview ?? staticHtml(result.text)} /></>
          : markdown ? <Documents.Provider value={context ? { ...context, base: path.split('/').slice(0, -1).join('/') } : null}><MarkdownMessage content={result.text} /></Documents.Provider>
          : <pre>{result.text}</pre>}
      </div>
    </section>
  </div>
}

function SpreadsheetPreview({ sheets, active, onChange, warning }: {
  sheets: TableSheet[]
  active: number
  onChange: (index: number) => void
  warning?: string
}) {
  const sheet = sheets[active]
  if (!sheet) return <div className="document-preview-empty"><strong>表格中没有可显示的数据</strong><span>可以下载原文件继续查看。</span></div>
  const truncated = sheet.totalRows > MAX_TABLE_ROWS || sheet.totalColumns > MAX_TABLE_COLUMNS
  return <div className="spreadsheet-preview">
    {sheets.length > 1 && <div className="spreadsheet-tabs" role="tablist" aria-label="工作表">
      {sheets.map((item, index) => <button type="button" role="tab" aria-selected={index === active}
        key={`${item.name}-${index}`} onClick={() => onChange(index)}>{item.name}</button>)}
    </div>}
    <div className="spreadsheet-summary">
      <span>{sheet.name} · {sheet.totalRows} 行 × {sheet.totalColumns} 列</span>
      {truncated && <span>为保证流畅，仅显示前 {MAX_TABLE_ROWS} 行、{MAX_TABLE_COLUMNS} 列。</span>}
      {warning && <span>{warning}</span>}
    </div>
    {sheet.rows.length ? <div className="spreadsheet-table-wrap">
      <table>
        <tbody>{sheet.rows.map((row, rowIndex) => <tr key={rowIndex}>
          <th scope="row">{rowIndex + 1}</th>
          {Array.from({ length: Math.min(sheet.totalColumns, MAX_TABLE_COLUMNS) }, (_, columnIndex) => (
            <td key={columnIndex}>{row[columnIndex] ?? ''}</td>
          ))}
        </tr>)}</tbody>
      </table>
    </div> : <div className="document-preview-empty"><strong>当前工作表为空</strong></div>}
  </div>
}
