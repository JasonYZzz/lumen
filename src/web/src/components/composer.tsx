'use client'

import { ArrowUp, FileCode, Stop } from '@phosphor-icons/react'
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react'
import { lumenApi } from '@/lib/api/client'
import type { FileSearchItem, QueueMode } from '@/lib/api/types'
import { filterSlashCommands, type SlashCommand } from '@/lib/slash-commands'

export interface ComposerProps {
  value: string
  busy: boolean
  queueMode: QueueMode
  slashCommands: SlashCommand[]
  onChange: (value: string) => void
  onQueueModeChange: (mode: QueueMode) => void
  onSubmit: () => void
  onStop: () => void
  onFocusChange?: (focused: boolean) => void
}

export function Composer({
  value,
  busy,
  queueMode,
  slashCommands,
  onChange,
  onQueueModeChange,
  onSubmit,
  onStop,
  onFocusChange,
}: ComposerProps) {
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [files, setFiles] = useState<FileSearchItem[]>([])
  const [commandIndex, setCommandIndex] = useState(0)
  const [commandsDismissed, setCommandsDismissed] = useState(false)
  const mention = useMemo(() => value.match(/(?:^|\s)(@[\w./~-]*)$/)?.[1] ?? null, [value])
  const matchingCommands = useMemo(
    () => commandsDismissed ? [] : filterSlashCommands(slashCommands, value),
    [commandsDismissed, slashCommands, value],
  )

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
      if (value.trim()) onSubmit()
    }
  }

  const chooseFile = (file: FileSearchItem) => {
    if (!mention) return
    const suffix = file.isDirectory ? '/' : ' '
    onChange(`${value.slice(0, -mention.length)}@${file.path}${suffix}`)
    setFiles([])
    textareaRef.current?.focus()
  }

  return (
    <div className="composer composer--workspace">
      <label className="sr-only" htmlFor="lumen-prompt">
        给 Lumen 发送消息
      </label>
      <textarea
        ref={textareaRef}
        id="lumen-prompt"
        className="composer-input"
        value={value}
        rows={1}
        placeholder={busy ? '补充指令…' : '输入任务或 @ 文件'}
        onChange={(event) => onChange(event.target.value)}
        onKeyDown={handleKeyDown}
        onFocus={() => onFocusChange?.(true)}
        onBlur={() => onFocusChange?.(false)}
        aria-expanded={matchingCommands.length > 0}
        aria-controls={matchingCommands.length > 0 ? 'slash-command-menu' : undefined}
        aria-activedescendant={matchingCommands.length > 0 ? `slash-command-${commandIndex}` : undefined}
      />

      {matchingCommands.length > 0 && (
        <div id="slash-command-menu" className="slash-commands" role="listbox" aria-label="命令">
          {matchingCommands.map((command, index) => (
            <button
              id={`slash-command-${index}`}
              key={command.value}
              type="button"
              role="option"
              aria-selected={index === commandIndex}
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
          {busy && (
            <button className="stop-button" type="button" onClick={onStop} aria-label="停止运行">
              <Stop size={13} weight="fill" />
              停止
            </button>
          )}
          <button
            className="send-button"
            type="button"
            disabled={!value.trim()}
            onClick={onSubmit}
            aria-label={busy ? '加入运行队列' : '发送消息'}
          >
            <ArrowUp size={19} weight="bold" />
          </button>
        </div>
      </div>
    </div>
  )
}
