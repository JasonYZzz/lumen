'use client'

import { ArrowSquareOut, Globe, X } from '@phosphor-icons/react'
import { createContext, useContext, useMemo, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import type { TimelineEntry } from '../lib/api/types'
import { markdownStructure } from '../lib/markdown-structure'
import { mergeSources, type WebSource } from '../lib/tool-output'
import { useModalFocus } from './use-modal-focus'

const Sources = createContext<{
  sources: WebSource[]
  open: (url?: string) => void
} | null>(null)

export function useTurnSources() { return useContext(Sources) }

export function sourceStatus(source: WebSource) {
  return [source.linked && '正文链接', source.read && '已阅读片段', source.retrieved && '已检索'].filter(Boolean).join(' · ')
}

export function TurnSourcesProvider({ entries, answer, children }: {
  entries: TimelineEntry[]; answer: string; children: ReactNode
}) {
  const sources = useMemo(() => mergeSources(entries, markdownStructure(answer).links), [entries, answer])
  const [selected, setSelected] = useState<string | null>(null)
  return <Sources.Provider value={{ sources, open: (url) => setSelected(url ?? '') }}>
    {children}
    {selected !== null && <SourcesPanel sources={sources} selected={selected} onClose={() => setSelected(null)} />}
  </Sources.Provider>
}

export function TurnSourcesSummary() {
  const context = useTurnSources()
  if (!context?.sources.length) return null
  const linked = context.sources.filter((source) => source.linked).length
  return <div className="turn-sources-summary">
    <button type="button" onClick={() => context.open()} aria-haspopup="dialog">
      <Globe size={16} aria-hidden="true" />来源与链接 <span>{context.sources.length}</span>
    </button>
    <small>{linked ? `${linked} 个正文链接` : '查看已检索与已阅读的资料'}</small>
  </div>
}

function SourcesPanel({ sources, selected, onClose }: {
  sources: WebSource[]; selected: string; onClose: () => void
}) {
  const ref = useModalFocus(onClose)
  const [filter, setFilter] = useState<'all' | 'linked' | 'read'>('all')
  const [current, setCurrent] = useState(selected)
  const visible = sources.filter((source) => filter === 'all' || (filter === 'linked' ? source.linked : source.read))
  const detail = sources.find((source) => source.url === current)
  return createPortal(<div className="sources-backdrop" onClick={(event) => {
    if (event.target === event.currentTarget) onClose()
  }}>
    <section ref={ref} className="sources-panel" role="dialog" aria-modal="true" aria-label="来源与链接" tabIndex={-1}>
      <header><div><strong>来源与链接</strong><small>{sources.length} 个页面</small></div>
        <button type="button" aria-label="关闭来源面板" onClick={onClose}><X size={20} /></button></header>
      <div className="sources-filters" role="group" aria-label="筛选来源">
        {([['all', '全部'], ['linked', '正文链接'], ['read', '已阅读片段']] as const).map(([value, label]) => (
          <button type="button" key={value} aria-pressed={filter === value} onClick={() => { setFilter(value); setCurrent('') }}>{label}</button>
        ))}
      </div>
      <div className="sources-panel-body">
        {detail && <article className="source-detail" aria-label="来源详情">
          <span>{sourceStatus(detail)}</span><h3>{detail.title}</h3>
          <p className="source-domain">{new URL(detail.url).hostname}{detail.published ? ` · ${detail.published}` : ''}</p>
          <p>{detail.snippet || '此页面只有正文链接，尚无可展示的检索摘要或阅读片段。'}</p>
          <a href={detail.url} target="_blank" rel="noopener noreferrer">打开原网页<ArrowSquareOut size={15} aria-hidden="true" /></a>
        </article>}
        {visible.length ? <ol className="sources-list">{visible.map((source) => <li key={source.url}>
          <button type="button" aria-current={current === source.url ? 'true' : undefined} onClick={() => setCurrent(source.url)}>
            <strong>{source.title}</strong><small>{new URL(source.url).hostname}{source.published ? ` · ${source.published}` : ''}</small>
            <span>{sourceStatus(source)}</span>{source.snippet && <p>{source.snippet.slice(0, 220)}</p>}
          </button>
        </li>)}</ol> : <p className="sources-empty">此筛选下没有来源。</p>}
      </div>
    </section>
  </div>, document.body)
}
