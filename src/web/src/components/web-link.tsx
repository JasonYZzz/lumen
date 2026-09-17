'use client'

import { Globe } from '@phosphor-icons/react'
import { useState, type ReactNode } from 'react'
import { safeWebUrl } from '../lib/tool-output'
import { sourceStatus, useTurnSources } from './turn-sources'

function SiteIcon({ origin }: { origin: string }) {
  const [failed, setFailed] = useState(false)
  return failed ? <Globe size={14} aria-hidden="true" />
    : <img src={`${origin}/favicon.ico`} width={14} height={14} alt="" loading="lazy"
      referrerPolicy="no-referrer" onError={() => setFailed(true)} />
}

export function WebLink({ href, children }: { href: string; children: ReactNode }) {
  const context = useTurnSources()
  let url: URL | null = null
  try { url = new URL(href) } catch { /* Local links retain their existing behavior. */ }
  if (!url || !['https:', 'http:'].includes(url.protocol) || url.username || url.password) {
    return <a href={href}>{children}</a>
  }
  const source = context?.sources.find((item) => item.url === safeWebUrl(href))
  const link = <a className={`web-source-link${source ? ' has-preview' : ''}`} href={href} target="_blank" rel="noopener noreferrer"
    aria-haspopup={source ? 'dialog' : undefined} onClick={source ? (event) => {
      if (event.button || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
      event.preventDefault()
      context?.open(source.url)
    } : undefined}>
    <span className="web-source-icon" aria-hidden="true"><SiteIcon key={url.origin} origin={url.origin} /></span>
    {children}
  </a>
  return source ? <span className="source-link-preview">{link}
    <span className="source-hover-preview" aria-hidden="true"><strong>{source.title}</strong>
      <span>{url.hostname}{source.published ? ` · ${source.published}` : ''}</span>
      <span>{sourceStatus(source)}</span>{source.snippet && <span>{source.snippet.slice(0, 180)}</span>}
    </span>
  </span> : link
}
