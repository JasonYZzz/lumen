'use client'

import { ArrowDown, ArrowClockwise, FileText, X } from '@phosphor-icons/react'
import { createContext, useContext, useEffect, useState, type ReactNode } from 'react'
import { lumenApi } from '../lib/api/client'
import type { TimelineEntry } from '../lib/api/types'
import { MarkdownMessage } from './markdown-message'
import { useModalFocus } from './use-modal-focus'

const extensions = /\.(md|markdown|txt|html?|json|csv|log|py|tsx?|jsx?|css|ya?ml|pdf|png|jpe?g|webp|gif|svg|docx|xlsx|pptx)$/i

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

function staticHtml(text: string): string {
  const document = new DOMParser().parseFromString(text, 'text/html')
  document.querySelectorAll('base, meta[http-equiv]').forEach((node) => node.remove())
  document.querySelectorAll('a[href], area[href]').forEach((node) => {
    if (!node.getAttribute('href')?.startsWith('#')) node.removeAttribute('href')
  })
  const policy = document.createElement('meta')
  policy.httpEquiv = 'Content-Security-Policy'
  policy.content = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; font-src data:; form-action 'none'; base-uri 'none'"
  document.head.prepend(policy)
  return document.documentElement.outerHTML
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
  const [result, setResult] = useState<{ text: string | null; url: string } | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const [source, setSource] = useState(false)
  const extension = path.split('.').at(-1)!.toLowerCase()
  const markdown = ['md', 'markdown'].includes(extension)
  const html = ['html', 'htm'].includes(extension)
  const image = ['png', 'jpg', 'jpeg', 'webp', 'gif'].includes(extension)
  const pdf = extension === 'pdf'
  const office = ['docx', 'xlsx', 'pptx'].includes(extension)
  useEffect(() => {
    const controller = new AbortController()
    let url: string | undefined
    setError('')
    setResult(null)
    void lumenApi.readDocument(path, controller.signal).then(async (blob) => {
      const text = !image && !pdf && !office && blob.size <= 1024 * 1024 ? await blob.text() : null
      if (controller.signal.aborted) return
      url = URL.createObjectURL(new Blob([blob], { type: pdf ? 'application/pdf' : image ? `image/${extension === 'jpg' ? 'jpeg' : extension}` : 'application/octet-stream' }))
      setResult({ text, url })
    }).catch((cause: unknown) => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : '读取失败') })
    return () => { controller.abort(); if (url) URL.revokeObjectURL(url) }
  }, [attempt, path, image, pdf, office, extension])
  const safeHtml = result?.text && html ? staticHtml(result.text) : ''
  return <div className="document-preview-backdrop" onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <section ref={ref} className="document-preview" role="dialog" aria-modal="true" aria-label={`文档预览：${path}`} tabIndex={-1}>
      <header><div><strong>{path.split('/').at(-1)}</strong><small>{path} · 当前文件</small></div>
        {result && result.text !== null && (markdown || html) && <button type="button" onClick={() => setSource(!source)}>{source ? '预览' : '源码'}</button>}
        {result && <a href={result.url} download={path.split('/').at(-1)}><ArrowDown size={16} />下载</a>}
        <button type="button" aria-label="关闭文档预览" onClick={onClose}><X size={20} /></button></header>
      <div className="document-preview-body">
        {error ? <div role="alert"><p>{error}</p><button type="button" onClick={() => setAttempt(attempt + 1)}><ArrowClockwise size={16} />重新读取</button></div>
          : !result ? <p role="status">正在读取文档…</p>
          : pdf ? <iframe title="PDF 预览" src={result.url} />
          : image ? <img src={result.url} alt={path.split('/').at(-1)} />
          : result.text === null ? <p>此格式或大小暂不支持内嵌预览，请下载后打开。</p>
          : source ? <pre>{result.text}</pre>
          : html ? <><p className="document-preview-note">静态预览：脚本和外部资源已禁用。</p><iframe title="HTML 静态预览" sandbox="" srcDoc={safeHtml} /></>
          : markdown ? <Documents.Provider value={context ? { ...context, base: path.split('/').slice(0, -1).join('/') } : null}><MarkdownMessage content={result.text} /></Documents.Provider>
          : <pre>{result.text}</pre>}
      </div>
    </section>
  </div>
}
