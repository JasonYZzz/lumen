'use client'

import { Globe } from '@phosphor-icons/react'
import { useState, type ReactNode } from 'react'

function SiteIcon({ origin }: { origin: string }) {
  const [failed, setFailed] = useState(false)
  return failed ? <Globe size={14} aria-hidden="true" />
    : <img src={`${origin}/favicon.ico`} width={14} height={14} alt="" loading="lazy"
      referrerPolicy="no-referrer" onError={() => setFailed(true)} />
}

export function WebLink({ href, children }: { href: string; children: ReactNode }) {
  let url: URL | null = null
  try { url = new URL(href) } catch { /* Local links retain their existing behavior. */ }
  if (!url || !['https:', 'http:'].includes(url.protocol) || url.username || url.password) {
    return <a href={href}>{children}</a>
  }
  return <a className="web-source-link" href={href} target="_blank" rel="noopener noreferrer">
    <span className="web-source-icon" aria-hidden="true"><SiteIcon key={url.origin} origin={url.origin} /></span>
    {children}
  </a>
}
