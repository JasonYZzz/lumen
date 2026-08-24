'use client'

import { ArrowUp, FileCode, Paperclip, Stop, X } from '@phosphor-icons/react'
import {
  useEffect,
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
import { tokenizePrompt } from '../lib/prompt-highlighting'
import { filterSlashCommands, type SlashCommand } from '../lib/slash-commands'

export interface ComposerProps {
  value: string
  busy: boolean
  queueMode: QueueMode
  slashCommands: SlashCommand[]
  attachments: AttachmentRef[]
  imageInputEnabled: boolean
  onChange: (value: string) => void
  onQueueModeChange: (mode: QueueMode) => void
  onAttachmentsChange: (attachments: AttachmentRef[]) => void
  onSubmit: () => void
  onStop: () => void
  onFocusChange?: (focused: boolean) => void
  liveControl?: ReactNode
  settingsControl?: ReactNode
}

export function Composer({
  value,
  busy,
  queueMode,
  slashCommands,
  attachments,
  imageInputEnabled,
  onChange,
  onQueueModeChange,
  onAttachmentsChange,
  onSubmit,
  onStop,
  onFocusChange,
  liveControl,
  settingsControl,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const highlightRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [files, setFiles] = useState<FileSearchItem[]>([])
  const [commandIndex, setCommandIndex] = useState(0)
  const [commandsDismissed, setCommandsDismissed] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [attachmentError, setAttachmentError] = useState<string | null>(null)
  const visibleAttachmentError = !imageInputEnabled && attachments.length
    ? '当前模型不支持已添加的图片，请切换到视觉模型或移除图片。'
    : attachmentError
  const mention = useMemo(() => value.match(/(?:^|\s)(@[\w./~-]*)$/)?.[1] ?? null, [value])
  const matchingCommands = useMemo(
    () => commandsDismissed ? [] : filterSlashCommands(slashCommands, value),
    [commandsDismissed, slashCommands, value],
  )
  const promptTokens = useMemo(() => tokenizePrompt(value), [value])

  useEffect(() => {
    const textarea = textareaRef.current
    if (!textarea) return
    textarea.style.height = 'auto'
    textarea.style.height = `${Math.min(textarea.scrollHeight, 154)}px`
  }, [value])

  useEffect(() => {
    if (!mention) {
      setFiles([])
      return
    }
    const timer = window.setTimeout(() => {
      lumenApi.searchFiles(mention).then(setFiles).catch(() => setFiles([]))
    }, 120)
    return () => window.clearTimeout(timer)
  }, [mention])

  useEffect(() => {
    setCommandIndex(0)
    setCommandsDismissed(false)
  }, [value])

  useEffect(() => {
    document.getElementById(`slash-command-${commandIndex}`)?.scrollIntoView({ block: 'nearest' })
  }, [commandIndex])

  const chooseCommand = (command: SlashCommand) => {
    onChange(command.value)
    setCommandsDismissed(true)
    window.requestAnimationFrame(() => textareaRef.current?.focus())
  }

  const handleKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (matchingCommands.length) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        const direction = event.key === 'ArrowDown' ? 1 : -1
        setCommandIndex((index) => (index + direction + matchingCommands.length) % matchingCommands.length)
        return
      }
      if (event.key === 'Tab') {
        event.preventDefault()
        chooseCommand(matchingCommands[commandIndex])
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        setCommandsDismissed(true)
        return
      }
      if (event.key === 'Enter' && !event.shiftKey) {
        const command = matchingCommands[commandIndex]
        if (value !== command.value.trimEnd() || command.value.endsWith(' ')) {
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
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      if (value.trim() || attachments.length) onSubmit()
    }
  }

  const chooseFile = (file: FileSearchItem) => {
    if (!mention) return
    const suffix = file.isDirectory ? '/' : ' '
    onChange(`${value.slice(0, -mention.length)}@${file.path}${suffix}`)
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
    <div className="composer-shell">
      {settingsControl && (
        <div className="composer-context" role="group" aria-label="任务设置">
          {settingsControl}
        </div>
      )}
      <div
        className="composer composer--workspace"
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
            placeholder={busy ? '补充指令…' : '输入任务、/ 命令或 @ 文件'}
            onChange={(event) => onChange(event.target.value)}
            onScroll={(event) => {
              if (highlightRef.current) highlightRef.current.scrollTop = event.currentTarget.scrollTop
            }}
            onKeyDown={handleKeyDown}
            onPaste={handlePaste}
            onFocus={() => onFocusChange?.(true)}
            onBlur={() => onFocusChange?.(false)}
            aria-expanded={matchingCommands.length > 0}
            aria-controls={matchingCommands.length > 0 ? 'slash-command-menu' : undefined}
            aria-activedescendant={matchingCommands.length > 0 ? `slash-command-${commandIndex}` : undefined}
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
                aria-selected={index === commandIndex}
                data-kind={command.kind ?? 'command'}
                className={index === commandIndex ? 'is-active' : ''}
                onMouseDown={(event) => event.preventDefault()}
                onMouseEnter={() => setCommandIndex(index)}
                onClick={() => chooseCommand(command)}
              >
                <code>{command.value.trimEnd()}</code>
                <span>{command.description}</span>
              </button>
            ))}
          </div>
        )}

        {files.length > 0 && (
          <div className="file-completions" role="listbox" aria-label="工作区文件">
            {files.slice(0, 8).map((file) => (
              <button key={file.path} type="button" role="option" onClick={() => chooseFile(file)}>
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
              className="attachment-button"
              disabled={!imageInputEnabled || uploading}
              onClick={() => fileInputRef.current?.click()}
              aria-label="添加图片"
              title={imageInputEnabled ? '添加图片' : '当前模型不支持图片输入'}
            >
              <Paperclip size={16} />
              {uploading ? '上传中' : '图片'}
            </button>
            {busy ? (
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
              <button className="stop-button" type="button" onClick={onStop} aria-label="停止运行">
                <Stop size={13} weight="fill" />
                停止
              </button>
            )}
            <button
              className="send-button"
              type="button"
              disabled={
                (!value.trim() && attachments.length === 0)
                || (!imageInputEnabled && attachments.length > 0)
                || uploading
              }
              onClick={onSubmit}
              aria-label={busy ? '加入运行队列' : '发送消息'}
            >
              <ArrowUp size={19} weight="bold" />
            </button>
          </div>
        </div>
      </div>
    </div>
  )
}
