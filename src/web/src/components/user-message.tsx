'use client'

import { CircleNotch, PencilSimpleLine } from '@phosphor-icons/react'
import { useLayoutEffect, useRef, useState } from 'react'
import type { TimelineEntry } from '@/lib/api/types'
import { CopyButton } from './copy-button'

export function UserMessage({ item, editDisabled, onEdit }: {
  item: TimelineEntry
  editDisabled?: string
  onEdit?: (item: TimelineEntry, text: string) => Promise<void>
}) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(item.text)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const editor = useRef<HTMLTextAreaElement>(null)
  const editButton = useRef<HTMLButtonElement>(null)
  const savingRef = useRef(false)
  useLayoutEffect(() => {
    if (editing) {
      editor.current?.focus({ preventScroll: true })
      const length = editor.current?.value.length ?? 0
      editor.current?.setSelectionRange(length, length)
      editor.current?.parentElement?.scrollIntoView({ block: 'nearest' })
    }
  // Focus only when opening; typing must not move the caret.
  }, [editing])
  const close = () => {
    setEditing(false)
    requestAnimationFrame(() => editButton.current?.focus({ preventScroll: true }))
  }
  const save = async () => {
    if (!onEdit || editDisabled || savingRef.current || !draft.trim() || draft.trim() === item.text.trim()) return
    savingRef.current = true
    setSaving(true)
    setError('')
    try {
      await onEdit(item, draft.trim())
      close()
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '重新生成失败，请重试。')
    } finally {
      savingRef.current = false
      setSaving(false)
    }
  }
  return <div className={`user-message${editing ? ' is-editing' : ''}`}>
    {editing ? <div className="message-editor">
      <textarea ref={editor} value={draft} aria-label="编辑消息" disabled={saving}
        onChange={(event) => setDraft(event.target.value)}
        onKeyDown={(event) => {
          if (event.nativeEvent.isComposing || saving) return
          if (event.key === 'Escape') { event.preventDefault(); close() }
          if (event.key === 'Enter' && (event.metaKey || event.ctrlKey)) { event.preventDefault(); void save() }
        }} />
      <p className="message-edit-hint">从这里重新生成，原对话保留。已执行的文件和外部操作不会回滚。{item.attachments?.length ? '原消息附件将保留。' : ''}</p>
      {error && <p className="message-edit-error" role="alert">{error}</p>}
      <div className="message-edit-actions">
        <button type="button" className="quiet" disabled={saving} onClick={close}>取消</button>
        <button type="button" disabled={saving || Boolean(editDisabled) || !draft.trim() || draft.trim() === item.text.trim()}
          onClick={() => void save()}>{saving && <CircleNotch size={15} className="spin" aria-hidden="true" />}{saving ? '正在重新生成…' : '发送并重新生成'}</button>
      </div>
      {editDisabled && <p className="message-edit-hint" role="status">{editDisabled}</p>}
    </div> : <>
      <article className="timeline-user">{item.text}</article>
      <div className="user-message-actions" aria-label="消息操作">
        <CopyButton text={item.text} label="复制消息" className="message-action" />
        {onEdit && <button type="button" className="message-action" ref={editButton} aria-label="编辑消息"
          title={editDisabled || '编辑消息'} disabled={Boolean(editDisabled)} onClick={() => {
            setDraft(item.text); setError(''); setEditing(true)
          }}><PencilSimpleLine size={16} aria-hidden="true" /></button>}
      </div>
    </>}
  </div>
}
