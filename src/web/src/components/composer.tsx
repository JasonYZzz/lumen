'use client'

import { ArrowUp, CaretRight, CircleNotch, Copy, FileCode, GitBranch, ListChecks, NotePencil, Paperclip, Plus, ShieldCheck, Sparkle, Stop, Terminal, X } from '@phosphor-icons/react'
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ClipboardEvent,
  type DragEvent,
  type KeyboardEvent,
  type ReactNode,
} from 'react'
import { lumenApi } from '../lib/api/client'
import type { AttachmentRef, FileSearchItem, QueueMode } from '../lib/api/types'
import { fileMentionAt, insertFileMention, tokenizePrompt } from '../lib/prompt-highlighting'
import { filterSlashCommands, type SlashCommand } from '../lib/slash-commands'

export interface ComposerProps {
  value: string
  busy: boolean
  stopping?: boolean
  stopError?: string
  queueMode: QueueMode
  slashCommands: SlashCommand[]
  attachments: AttachmentRef[]
  imageInputEnabled: boolean
  onChange: (value: string) => void
  onQueueModeChange: (mode: QueueMode) => void
  onAttachmentsChange: (attachments: AttachmentRef[]) => void
  onSubmit: () => void
  onCommand?: (command: string, preserveDraft?: boolean) => void
  onStop: () => void
  onFocusChange?: (focused: boolean) => void
  liveControl?: ReactNode
  settingsControl?: ReactNode
}

export function Composer({
  value,
  busy,
  stopping = false,
  stopError,
  queueMode,
  slashCommands,
  attachments,
  imageInputEnabled,
  onChange,
  onQueueModeChange,
  onAttachmentsChange,
  onSubmit,
  onCommand,
  onStop,
  onFocusChange,
  liveControl,
  settingsControl,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const highlightRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const shellRef = useRef<HTMLDivElement>(null)
  const pendingCaret = useRef<number | null>(null)
  const [selection, setSelection] = useState({ start: value.length, end: value.length })
  const [files, setFiles] = useState<FileSearchItem[]>([])
  const [fileIndex, setFileIndex] = useState(0)
  const [commandIndex, setCommandIndex] = useState(0)
  const [commandsDismissed, setCommandsDismissed] = useState(false)
  const [actionsOpen, setActionsOpen] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const visibleAttachmentError = !imageInputEnabled && attachments.length
    ? '当前模型不支持已添加的图片，请切换到视觉模型或移除图片。'
    : attachmentError
  const mention = useMemo(
    () => fileMentionAt(value, Math.min(selection.start, value.length), Math.min(selection.end, value.length)),
    [value, selection.start, selection.end],
  )
  const matchingCommands = useMemo(
    () => actionsOpen ? [{ value: 'attach-image', label: '添加图片',
      description: imageInputEnabled ? '从电脑上传，也可以直接粘贴' : '需要支持图片的模型' },
    ...filterSlashCommands(slashCommands, '/').filter((command) =>
      ['/model', '/mode', '/tasks', '/copy', '/new', '/checkpoints', '/skills', '/tools'].includes(command.value))]
      : commandsDismissed ? [] : filterSlashCommands(slashCommands, value),
    [actionsOpen, commandsDismissed, imageInputEnabled, slashCommands, value],
  )
  const promptTokens = useMemo(() => tokenizePrompt(value), [value])

  const syncHighlight = () => {
    const textarea = textareaRef.current
    const highlight = highlightRef.current
    if (!textarea || !highlight) return
    highlight.style.width = `${textarea.clientWidth}px`
    highlight.scrollTop = textarea.scrollTop
    highlight.scrollLeft = textarea.scrollLeft
  }

  useLayoutEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 154)}px`
    if (pendingCaret.current !== null) {
      textarea.focus()
      textarea.setSelectionRange(pendingCaret.current, pendingCaret.current)
      pendingCaret.current = null
    }
    syncHighlight()
  }, [value, selection.start, selection.end])

  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    const observer = new ResizeObserver(syncHighlight)
    observer.observe(textarea)
    return () => observer.disconnect()
  }, [])

  useEffect(() => {
    let current = true
    setFiles([])
    setFileIndex(0)
    if (!mention) {
      return
    }
    const timer = window.setTimeout(() => {
      lumenApi.searchFiles(`@${mention.query}`)
        .then((results) => { if (current) setFiles(results.slice(0, 8)) })
        .catch(() => { if (current) setFiles([]) })
    }, 120)
    return () => { current = false; window.clearTimeout(timer) }
  }, [mention])

  useEffect(() => {
    setCommandIndex(0)
    setCommandsDismissed(false)
    setActionsOpen(false)
  }, [value])

  useEffect(() => {
    if (!actionsOpen && !matchingCommands.length) return
    const dismiss = (event: PointerEvent) => {
      if (!shellRef.current?.contains(event.target as Node)) {
        setActionsOpen(false)
        setCommandsDismissed(true)
      }
    }
    document.addEventListener('pointerdown', dismiss)
    return () => document.removeEventListener('pointerdown', dismiss)
  }, [actionsOpen, matchingCommands.length])

  useEffect(() => {
    document.getElementById(`slash-command-${commandIndex}`)?.scrollIntoView({ block: 'nearest' })
  }, [commandIndex])

  const chooseCommand = (command: SlashCommand, completeOnly = false) => {
    if (command.value === 'attach-image') {
      if (!imageInputEnabled || uploading) return
      setActionsOpen(false)
      fileInputRef.current?.click()
      return
    }
    if (!completeOnly && onCommand && !command.value.endsWith(' ')) {
      setCommandsDismissed(true)
      setActionsOpen(false)
      onCommand(command.value, actionsOpen)
      return
    }
    pendingCaret.current = command.value.length
    setSelection({ start: command.value.length, end: command.value.length })
    onChange(command.value)
    setActionsOpen(false)
    setCommandsDismissed(true)
    window.requestAnimationFrame(() => textareaRef.current?.focus())
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing || event.keyCode === 229) return
    if (files.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        setFileIndex((index) => (index + (event.key === 'ArrowDown' ? 1 : -1) + files.length) % files.length)
        return
      }
      if (event.key === 'Tab' || (event.key === 'Enter' && !event.shiftKey)) {
        event.preventDefault()
        chooseFile(files[fileIndex])
        return
      }
    }
    if (matchingCommands.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        const direction = event.key === 'ArrowDown' ? 1 : -1
        setCommandIndex((index) => (index + direction + matchingCommands.length) % matchingCommands.length)
        return
      }
      if (event.key === 'Tab') {
        event.preventDefault()
        chooseCommand(matchingCommands[Math.min(commandIndex, matchingCommands.length - 1)], true)
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setCommandsDismissed(true)
        setActionsOpen(false)
        return
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        const command = matchingCommands[Math.min(commandIndex, matchingCommands.length - 1)]
        if (onCommand || value !== command.value.trimEnd() || command.value.endsWith(' ')) {
          event.preventDefault()
          chooseCommand(command)
          return
        }
      }
    }
    if (event.key === 'Escape' && files.length) {
      event.preventDefault()
      setFiles([])
      return
    }
    if (event.key === 'Enter' && !event.shiftKey && !event.nativeEvent.isComposing && !stopping) {
      event.preventDefault()
      if (!uploading && (value.trim().startsWith('/') || imageInputEnabled || !attachments.length)
        && (value.trim() || attachments.length)) onSubmit()
    }
  }

  const chooseFile = (file: FileSearchItem) => {
    if (!mention) return
    const insertion = insertFileMention(value, mention, file.path, file.isDirectory)
    pendingCaret.current = insertion.caret
    setSelection({ start: insertion.caret, end: insertion.caret })
    onChange(insertion.text)
    setFiles([])
    textareaRef.current?.focus()
  }

  const addImages = async (selected: File[]) => {
    if (!imageInputEnabled) {
      setAttachmentError('当前模型不支持图片输入，请先切换到视觉模型。')
      return
    }
    const images = selected.filter((file) => file.type.startsWith('image/'))
    if (!images.length) return
    if (attachments.length + images.length > 8) {
      setAttachmentError('每条消息最多添加 8 张图片。')
      return
    }
    setUploading(true)
    setAttachmentError(null)
    try {
      const uploaded = await Promise.all(images.map((file) => lumenApi.uploadAttachment(file)))
      onAttachmentsChange([...attachments, ...uploaded])
    } catch (error) {
      setAttachmentError(error instanceof Error ? error.message : '图片上传失败')
    } finally {
      setUploading(false)
    }
  }

  const handlePaste = (event: ClipboardEvent<HTMLTextAreaElement>) => {
    const images = Array.from(event.clipboardData.items)
      .filter((item) => item.kind === 'file' && item.type.startsWith('image/'))
      .map((item) => item.getAsFile())
      .filter((file): file is File => file !== null)
    if (!images.length) return
    event.preventDefault()
    void addImages(images)
  }

  const handleDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault()
    void addImages(Array.from(event.dataTransfer.files))
  }

  return (
    <div ref={shellRef} className="composer-shell">
      {settingsControl && (
        <div className="composer-context" role="group" aria-label="任务设置">
          {settingsControl}
        </div>
      )}
      <div
        className={`composer composer--workspace ${attachments.length || visibleAttachmentError ? 'has-attachments' : ''}`}
        onDragOver={(event) => event.preventDefault()}
        onDrop={handleDrop}
      >
        <label className="sr-only" htmlFor="lumen-prompt">
          给 Lumen 发送消息
        </label>
        <div className="composer-editor">
          <div ref={highlightRef} className="composer-highlight" aria-hidden="true">
            {promptTokens.map((token, index) => (
              <span key={`${index}-${token.value}`} className={`prompt-token-${token.kind}`}>
                {token.value}
              </span>
            ))}
            {value.endsWith('\n') && '\n'}
          </div>
          <textarea
            ref={textareaRef}
            id="lumen-prompt"
            className="composer-input"
            value={value}
            rows={1}
            placeholder={busy ? '补充指令…' : '给 Lumen 一个任务'}
            onChange={(event) => {
              setSelection({ start: event.target.selectionStart, end: event.target.selectionEnd })
              onChange(event.target.value)
            }}
            onSelect={(event) => setSelection({
              start: event.currentTarget.selectionStart, end: event.currentTarget.selectionEnd,
            })}
            onScroll={syncHighlight}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onFocus={() => onFocusChange?.(true)}
            onBlur={() => onFocusChange?.(false)}
            aria-expanded={matchingCommands.length > 0 || files.length > 0}
            aria-controls={files.length ? 'file-completion-menu' : matchingCommands.length ? 'slash-command-menu' : undefined}
            aria-activedescendant={files.length ? `file-completion-${fileIndex}` : matchingCommands.length ? `slash-command-${commandIndex}` : undefined}
          />
        </div>

        {(attachments.length > 0 || visibleAttachmentError) && (
          <div className="composer-attachments" aria-live="polite">
            {attachments.map((attachment) => (
              <span key={attachment.artifactRef} className="composer-attachment">
                <span>{attachment.filename}</span>
                <button
                  type="button"
                  aria-label={`移除 ${attachment.filename}`}
                  onClick={() => onAttachmentsChange(
                    attachments.filter((item) => item.artifactRef !== attachment.artifactRef),
                  )}
                >
                  <X size={12} />
                </button>
              </span>
            ))}
            {visibleAttachmentError && (
              <span className="composer-attachment-error">{visibleAttachmentError}</span>
            )}
          </div>
        )}

        {matchingCommands.length > 0 && (
          <div id="slash-command-menu" className="slash-commands" role="listbox" aria-label="命令">
            {matchingCommands.map((command, index) => (
              <button
                id={`slash-command-${index}`}
                key={command.value}
                type="button"
                role="option"
                disabled={command.value === 'attach-image' && (!imageInputEnabled || uploading)}
                aria-selected={index === commandIndex}
                data-kind={command.kind ?? 'command'}
                title={command.description}
                className={index === commandIndex ? 'is-active' : ''}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setCommandIndex(index)}
                onClick={() => chooseCommand(command)}
              >
                <CommandIcon command={command} />
                <strong>{command.label ?? command.description}</strong>
                <span>{command.label ? command.description : command.value.trimEnd()}</span>
                {['/model', '/mode'].includes(command.value) ? <CaretRight size={16} aria-hidden="true" />
                  : command.label && command.kind !== 'skill' && command.value.startsWith('/') && <code>{command.value.trimEnd()}</code>}
              </button>
            ))}
            <div className="slash-menu-hint">↑↓ 选择 · Enter 打开 · Tab 补全 · Esc 关闭</div>
          </div>
        )}

        {files.length > 0 && (
          <div id="file-completion-menu" className="file-completions" role="listbox" aria-label="工作区文件">
            {files.map((file, index) => (
              <button key={file.path} id={`file-completion-${index}`} type="button" role="option"
                aria-selected={index === fileIndex} className={index === fileIndex ? 'is-active' : ''}
                onMouseDown={(event) => event.preventDefault()} onMouseEnter={() => setFileIndex(index)}
                onClick={() => chooseFile(file)}>
                <FileCode size={16} />
                <span>{file.name}{file.isDirectory ? '/' : ''}</span>
                <small>{file.path}</small>
              </button>
            ))}
          </div>
        )}

        <div className="composer-toolbar">
          <div className="composer-tools">
            <input
              ref={fileInputRef}
              type="file"
              accept="image/png,image/jpeg,image/gif,image/webp"
              multiple
              hidden
              disabled={!imageInputEnabled || uploading}
              onChange={(event) => {
                void addImages(Array.from(event.target.files ?? []))
                event.target.value = ''
              }}
            />
            <button
              type="button"
              className="composer-add-button"
              onClick={() => {
                setActionsOpen(!actionsOpen)
                setCommandIndex(imageInputEnabled ? 0 : 1)
                textareaRef.current?.focus()
              }}
              aria-label="添加内容与快捷操作"
              title="添加内容与快捷操作"
              aria-haspopup="listbox"
              aria-expanded={actionsOpen}
              aria-controls={actionsOpen ? 'slash-command-menu' : undefined}
            >
              <Plus size={22} aria-hidden="true" />
            </button>
            {busy && (value.trim() || attachments.length > 0) ? (
              <div className="queue-mode" aria-label="运行中输入方式">
                <button
                  type="button"
                  className={queueMode === 'steer' ? 'is-active' : ''}
                  onClick={() => onQueueModeChange('steer')}
                >
                  立即补充
                </button>
                <button
                  type="button"
                  className={queueMode === 'follow_up' ? 'is-active' : ''}
                  onClick={() => onQueueModeChange('follow_up')}
                >
                  完成后继续
                </button>
              </div>
            ) : (
              <span className="composer-hint">@ 文件&nbsp;&nbsp; / 命令</span>
            )}
          </div>

          <div className="composer-actions">
            {liveControl}
            {busy && (
              <button className="stop-button" type="button" onClick={onStop} disabled={stopping}
                aria-label={stopping ? '正在停止' : '停止运行'} title={stopping ? '正在停止…' : '停止生成'}>
                {stopping ? <CircleNotch size={19} className="spin" aria-hidden="true" /> : <Stop size={16} weight="fill" aria-hidden="true" />}
              </button>
            )}
            {(!busy || value.trim() || attachments.length > 0) && (
            <button
              className="send-button"
              type="button"
              disabled={
                (!value.trim() && attachments.length === 0)
                || (!value.trim().startsWith('/') && !imageInputEnabled && attachments.length > 0)
                || uploading
                || stopping
              }
              onClick={onSubmit}
              aria-label={busy ? '加入运行队列' : '发送消息'}
            >
              <ArrowUp size={19} weight="bold" />
            </button>
            )}
          </div>
        </div>
      </div>
      {stopError && <p className="composer-stop-error" role="alert">{stopError}</p>}
    </div>
  )
}

function CommandIcon({ command }: { command: SlashCommand }) {
  const name = command.value.split(' ')[0]
  const Icon = name === 'attach-image' ? Paperclip : name === '/model' ? Sparkle : name === '/tasks' || name === '/plan' ? ListChecks
    : name === '/mode' ? ShieldCheck : name === '/copy' ? Copy : name === '/new' ? NotePencil
      : name === '/checkpoints' ? GitBranch : command.kind === 'skill' ? Sparkle : Terminal
  return <Icon size={21} aria-hidden="true" />
}
