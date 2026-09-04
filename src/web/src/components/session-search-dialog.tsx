import { ChatCircle, X } from '@phosphor-icons/react'
import { useEffect, useId, useRef, useState, type KeyboardEvent, type RefObject } from 'react'
import type { SessionSummary } from '@/lib/api/types'
import { useModalFocus } from './use-modal-focus'

export function SessionSearchDialog({ sessions, loading, onSelect, onClose, returnFocusRef }: {
  sessions: SessionSummary[]
  loading: boolean
  onSelect: (sessionId: string) => void
  onClose: () => void
  returnFocusRef?: RefObject<HTMLElement | null>
}) {
  const [query, setQuery] = useState('')
  const [activeId, setActiveId] = useState<string | null>(null)
  const inputRef = useRef<HTMLInputElement>(null)
  const resultsRef = useRef<HTMLDivElement>(null)
  const dialogRef = useModalFocus(onClose, true, returnFocusRef)
  const listId = useId()
  const keyword = query.trim().toLocaleLowerCase()
  // The Host catalog owns titles and recency; the dialog only filters its projection.
  const results = sessions.filter((session) => keyword
    ? session.title.toLocaleLowerCase().includes(keyword)
    : !session.archived)
  const activeIndex = results.findIndex((session) => session.sessionId === activeId)
  const optionId = (index: number) => `${listId}-${index}`

  useEffect(() => { inputRef.current?.focus({ preventScroll: true }) }, [])

  const handleKeyDown = (event: KeyboardEvent<HTMLInputElement>) => {
    if (event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229 || !results.length) return
    if (event.key === 'Enter') {
      event.preventDefault()
      onSelect(results[Math.max(0, activeIndex)].sessionId)
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault()
      const next = activeIndex < 0 ? (event.key === 'ArrowDown' ? 0 : results.length - 1)
        : (activeIndex + (event.key === 'ArrowDown' ? 1 : -1) + results.length) % results.length
      setActiveId(results[next].sessionId)
      document.getElementById(optionId(next))?.scrollIntoView({ block: 'nearest' })
    }
  }

  return (
    <div className="session-search-overlay" onClick={(event) => {
      if (event.target === event.currentTarget) onClose()
    }}>
      <div className="session-search-dialog" ref={dialogRef} role="dialog" aria-modal="true"
        aria-label="搜索对话" tabIndex={-1}>
        <div className="session-search-header">
          <input ref={inputRef} role="combobox" aria-label="搜索对话标题" placeholder="搜索…"
            aria-autocomplete="list" aria-expanded="true" aria-controls={listId}
            aria-activedescendant={activeIndex >= 0 ? optionId(activeIndex) : undefined}
            autoComplete="off" spellCheck={false} value={query} onKeyDown={handleKeyDown}
            onChange={(event) => {
              setQuery(event.target.value)
              setActiveId(null)
              if (resultsRef.current) resultsRef.current.scrollTop = 0
            }} />
          <button type="button" aria-label="关闭搜索" title="关闭搜索" onClick={onClose}>
            <X size={22} aria-hidden="true" />
          </button>
        </div>
        <div className="session-search-results" ref={resultsRef}>
          <p className="session-search-heading" id={`${listId}-label`}>{keyword ? '搜索结果' : '最近对话'}</p>
          <div id={listId} role="listbox" aria-labelledby={`${listId}-label`} aria-busy={loading}>
            {results.map((session, index) => (
              <div key={session.sessionId} id={optionId(index)} role="option"
                aria-selected={index === activeIndex} className="session-search-result" title={session.title}
                onPointerMove={() => setActiveId(session.sessionId)}
                onMouseDown={(event) => event.preventDefault()}
                onClick={() => onSelect(session.sessionId)}>
                <ChatCircle size={22} aria-hidden="true" />
                <span className="session-search-title">{session.title}</span>
                {session.archived && <span className="session-search-archived">已归档</span>}
              </div>
            ))}
          </div>
          <p role="status" className={results.length ? 'sr-only' : 'session-search-empty'}>
            {results.length ? `${results.length} 个对话` : loading ? '正在加载对话…'
              : keyword ? '没有找到相关对话，试试其他关键词。' : '还没有最近对话。'}
          </p>
        </div>
      </div>
    </div>
  )
}
