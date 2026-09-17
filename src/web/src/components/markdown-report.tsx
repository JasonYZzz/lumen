'use client'

import { BookOpen, X } from '@phosphor-icons/react'
import { useId, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { TimelineEntry } from '../lib/api/types'
import { markdownStructure } from '../lib/markdown-structure'
import { MarkdownMessage } from './markdown-message'
import { TurnSourcesProvider, TurnSourcesSummary } from './turn-sources'
import { useModalFocus } from './use-modal-focus'

export function MarkdownReport({ content, entries = [] }: { content: string; entries?: TimelineEntry[] }) {
  const structure = useMemo(() => markdownStructure(content), [content])
  const prefix = useId()
  const root = useRef<HTMLDivElement>(null)
  const ids = structure.headings.map((heading) => `${prefix}-${heading.id}`)
  return <TurnSourcesProvider entries={entries} answer={content}>
    <div className="markdown-report" ref={root}>
      {structure.headings.length > 1 && <nav className="report-contents" aria-label="报告目录">
        <strong>目录</strong><ol>{structure.headings.map((heading, index) => <li key={ids[index]}>
          <button type="button" style={{ paddingLeft: `${Math.min(heading.depth - 1, 3) * 10 + 8}px` }} onClick={() => {
            const target = root.current?.querySelector<HTMLElement>(`[id="${ids[index]}"]`)
            target?.scrollIntoView({ behavior: window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth', block: 'start' })
            target?.focus({ preventScroll: true })
          }}>{heading.title}</button>
        </li>)}</ol>
      </nav>}
      <div className="report-reading"><MarkdownMessage content={content} headingIds={ids} /><TurnSourcesSummary /></div>
    </div>
  </TurnSourcesProvider>
}

export function ReportReadingButton({ content, entries }: { content: string; entries: TimelineEntry[] }) {
  const [open, setOpen] = useState(false)
  if (content.length < 2500 && markdownStructure(content).headings.length < 3) return null
  return <><button type="button" className="report-reading-button" aria-haspopup="dialog" onClick={() => setOpen(true)}>
    <BookOpen size={16} aria-hidden="true" />阅读完整回答</button>
    {open && <ReportDialog content={content} entries={entries} onClose={() => setOpen(false)} />}
  </>
}

function ReportDialog({ content, entries, onClose }: { content: string; entries: TimelineEntry[]; onClose: () => void }) {
  const ref = useModalFocus(onClose)
  return createPortal(<div className="report-backdrop" onClick={(event) => { if (event.target === event.currentTarget) onClose() }}>
    <div ref={ref} className="report-dialog" role="dialog" aria-modal="true" aria-label="完整回答" tabIndex={-1}>
      <header><strong>完整回答</strong><button type="button" onClick={onClose} aria-label="关闭阅读视图"><X size={20} /></button></header>
      <div className="report-dialog-body"><MarkdownReport content={content} entries={entries} /></div>
    </div>
  </div>, document.body)
}
