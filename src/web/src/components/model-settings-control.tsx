'use client'

import { ArrowCounterClockwise, ArrowLeft, CaretDown, Check, MagnifyingGlass } from '@phosphor-icons/react'
import { useEffect, useId, useRef, useState, type CSSProperties } from 'react'
import { createPortal } from 'react-dom'
import type { ReasoningLevel } from '@/lib/api/types'
import type { ChoiceOption } from './choice-menu'
import { useAnchoredPopover } from './use-anchored-popover'
import { ModelIcon } from './model-icon'

const names: Partial<Record<ReasoningLevel, string>> = {
  off: '关闭', minimal: '最少', low: '低', medium: '中', high: '高', xhigh: '很高', max: '最高',
}
const levelName = (level: string) => names[level as ReasoningLevel] ?? level
const optionName = (option?: ChoiceOption<string>) => option?.label.includes(' → ')
  ? option.label.split(' → ').map(levelName).join(' → ') : levelName(option?.value ?? '')
export type ModelSettingsView = 'settings' | 'models' | null

export function modelDisplayName(modelId: string | null | undefined, fallback = '') {
  return modelId?.split(':').pop() || fallback
}

export function ModelSettingsControl({ value, options, model, modelId, models, modelIds, defaultLevel, view, onViewChange,
  disabled, reasoningDisabled, description, modelNotice, pendingModel, onChange, onModelChange }: {
  value: string; options: ChoiceOption<string>[]; model: string; modelId?: string
  defaultLevel?: ReasoningLevel | null
  models: string[]; view: ModelSettingsView; onViewChange: (view: ModelSettingsView) => void
  modelIds?: Record<string, string>
  disabled: boolean; reasoningDisabled: boolean; description: string; modelNotice: string
  pendingModel?: string | null
  onChange: (level: string) => Promise<void>; onModelChange: (model: string) => Promise<void>
}) {
  const open = view !== null
  const viewRef = useRef(view)
  viewRef.current = view
  useEffect(() => {viewRef.current = view; return () => {viewRef.current = null}}, [])
  const trigger = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)
  const range = useRef<HTMLInputElement>(null)
  const modelButton = useRef<HTMLButtonElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const id = useId()
  const levels = options.filter(option => !['provider_default', 'configured'].includes(option.value))
  const [preview, setPreview] = useState<string | null>(null)
  const [pending, setPending] = useState(false)
  const [error, setError] = useState('')
  const [query, setQuery] = useState('')
  const selectedModel = pendingModel ?? model
  const displayName = (name: string) => modelDisplayName((name === model ? modelId : undefined) ?? modelIds?.[name], name)
  const shownModel = displayName(selectedModel)
  const matches = models.filter(name => `${name} ${displayName(name)}`.toLowerCase().includes(query.trim().toLowerCase()))
  const saving = useRef(false)
  const selected = options.find(option => option.value === value)
  const current = preview ?? (value === 'provider_default' ? defaultLevel : value)
  const index = levels.findIndex(option => option.value === current)
  const position = useAnchoredPopover({open, anchor: trigger.current, popoverRef: panel, width: 360})
  const close = (focus = false) => {
    onViewChange(null)
    if (focus) trigger.current?.focus({preventScroll: true})
  }
  useEffect(() => {
    if (!open) return
    setPreview(null); setError('')
    setQuery('')
    const frame = requestAnimationFrame(() => (view === 'models' ? search.current : modelButton.current)?.focus({preventScroll: true}))
    const outside = (event: PointerEvent) => {
      if (!panel.current?.contains(event.target as Node) && !trigger.current?.contains(event.target as Node)) close()
    }
    const escape = (event: KeyboardEvent) => {
      if (event.isComposing || event.keyCode === 229) return
      if (event.key === 'Escape') { event.preventDefault(); close(true) }
    }
    window.addEventListener('pointerdown', outside, true)
    window.addEventListener('keydown', escape)
    return () => {
      cancelAnimationFrame(frame)
      window.removeEventListener('pointerdown', outside, true)
      window.removeEventListener('keydown', escape)
    }
  }, [view])
  const save = async (level: string) => {
    if (saving.current || disabled || reasoningDisabled) return
    if (level === value) { setPreview(null); return }
    saving.current = true; setPending(true); setError('')
    try { await onChange(level); setPreview(null) }
    catch (error) { setPreview(null); setError(error instanceof Error ? error.message : '切换失败，请重试') }
    finally { saving.current = false; setPending(false) }
  }
  const caption = !levels.length ? selected?.compactLabel ?? selected?.label ?? '未配置'
    : value === 'provider_default' ? `默认${defaultLevel ? ` · ${names[defaultLevel] ?? defaultLevel}` : ''}`
      : value === 'configured' ? '原始配置' : optionName(selected)
  const selectModel = async (name: string) => {
    if (saving.current || disabled) return
    saving.current = true; setPending(true); setError('')
    try {
      if (name !== selectedModel) await onModelChange(name)
      if (viewRef.current === 'models') onViewChange('settings')
    } catch (error) { setError(error instanceof Error ? error.message : '模型切换失败，请重试') }
    finally { saving.current = false; setPending(false) }
  }
  return <div className="choice-menu is-model model-settings-control">
    <button ref={trigger} type="button" className="choice-menu-trigger" disabled={disabled}
      aria-label={`模型：${shownModel || '未配置'}；推理强度：${selected?.label ?? value}`} aria-haspopup="dialog" aria-expanded={open}
      aria-controls={open ? id : undefined} title={`${shownModel} · ${pendingModel ? '待切换' : caption}`}
      onClick={() => onViewChange(open ? null : 'settings')}
      onKeyDown={event => {if (['ArrowDown', 'ArrowUp'].includes(event.key)) {event.preventDefault(); onViewChange('settings')}}}>
      <span className="choice-menu-icon" aria-hidden="true"><ModelIcon model={shownModel} /></span>
      <span className="choice-menu-value">{shownModel || '未配置'}</span>
      {(pendingModel || levels.length > 0 || value === 'configured') && <span className="model-effort-summary">{pendingModel ? '待切换'
        : value === 'provider_default' ? <>默认<span className="model-default-level">{defaultLevel ? ` · ${levelName(defaultLevel)}` : ''}</span></> : caption}</span>}
      <CaretDown size={12} className="choice-menu-caret" />
    </button>
    {open && createPortal(<div ref={panel} id={id} role="dialog" aria-label="模型与思考"
      className="choice-menu-popover model-settings-panel" style={position}
      onKeyDown={event => {
        if (event.nativeEvent.isComposing || event.keyCode === 229) return
        if (event.key === 'Tab') {
          const items = Array.from(panel.current?.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), summary') ?? [])
            .filter(item => item.tabIndex >= 0)
          if (view === 'models' && document.activeElement?.getAttribute('role') === 'option') {
            event.preventDefault()
            if (event.shiftKey) search.current?.focus()
            else close(true)
            return
          }
          if (document.activeElement === items[event.shiftKey ? 0 : items.length - 1]) {event.preventDefault(); close(true)}
        }
        if (view !== 'models') return
        const items = Array.from(panel.current?.querySelectorAll<HTMLButtonElement>('[role="option"]') ?? [])
        const current = items.indexOf(document.activeElement as HTMLButtonElement)
        let next = current
        if (event.key === 'ArrowDown') next = (current + 1) % items.length
        else if (event.key === 'ArrowUp') next = current < 0 ? items.length - 1 : (current - 1 + items.length) % items.length
        else if (event.key === 'Home' && current >= 0) next = 0
        else if (event.key === 'End' && current >= 0) next = items.length - 1
        else if (event.key === 'Enter' && document.activeElement === search.current) {event.preventDefault(); items[0]?.click(); return}
        else return
        event.preventDefault(); items[next]?.focus()
      }}>
      {view === 'models' ? <>
        <button type="button" className="model-settings-back" onClick={() => onViewChange('settings')}><ArrowLeft size={16} />模型与思考</button>
        <label className="choice-menu-search"><MagnifyingGlass size={17} aria-hidden="true" />
          <input ref={search} value={query} onChange={event => setQuery(event.target.value)} aria-label="搜索模型" placeholder="搜索已配置模型" aria-controls={`${id}-models`} /></label>
        <div id={`${id}-models`} className="model-settings-list" role="listbox" aria-label="模型" aria-busy={pending}>
          {matches.map(name => <button type="button" key={name} role="option" aria-selected={name === selectedModel}
            tabIndex={-1} className="choice-menu-option" disabled={pending || disabled} onClick={() => void selectModel(name)}>
            <span className="choice-option-icon" aria-hidden="true"><ModelIcon model={displayName(name)} /></span>
            <span><strong>{displayName(name)}</strong>{displayName(name) !== name && <small>配置：{name}</small>}</span>{name === selectedModel && <Check size={16} aria-hidden="true" />}
          </button>)}
        </div>
        {!matches.length && <p className="reasoning-note">{models.length ? '没有匹配项，请尝试其他名称' : '暂无已配置模型'}</p>}
      </> : <>
      <button ref={modelButton} type="button" className="model-settings-model" aria-label="切换模型" disabled={pending || disabled}
        onClick={() => onViewChange('models')}>
        <span aria-hidden="true"><ModelIcon model={shownModel} /></span><strong>{shownModel || '未配置'}</strong><CaretDown size={15} />
      </button>
      <section className="model-settings-reasoning" aria-label="思考强度">
      <div className="reasoning-heading">
        <span>思考强度</span>
        <output aria-live="polite">{pendingModel ? '等待切换' : preview ? optionName(levels.find(option => option.value === preview)) : caption}</output>
        <button type="button" aria-label="恢复供应商默认" title="恢复供应商默认" disabled={pending || disabled || reasoningDisabled || value === 'provider_default'}
          onClick={() => void save('provider_default')}><ArrowCounterClockwise size={20} /></button>
      </div>
      {levels.length > 0 && !pendingModel && <>
      <div className={`reasoning-scale ${index < 0 ? 'is-unset' : ''}`}
        style={{'--effort-fill': `${index < 0 || levels.length < 2 ? 0 : index / (levels.length - 1) * 100}%`} as CSSProperties}>
        <div className="reasoning-track" aria-hidden="true">{levels.map(option => <i key={option.value} />)}</div>
        <input ref={range} type="range" min={0} max={Math.max(0, levels.length - 1)} step={1}
          aria-label="思考强度档位" aria-valuetext={index < 0 ? caption : levels[index].label}
          value={Math.max(0, index)} disabled={pending || disabled || reasoningDisabled}
          onChange={event => setPreview(levels[Number(event.target.value)]?.value ?? null)}
          onPointerUp={event => void save(levels[Number(event.currentTarget.value)].value)}
          onPointerCancel={() => setPreview(null)}
          onKeyUp={event => {
            if (['ArrowLeft', 'ArrowRight', 'ArrowUp', 'ArrowDown', 'Home', 'End'].includes(event.key))
              void save(levels[Number(event.currentTarget.value)].value)
          }} />
      </div>
      <div className="reasoning-stops" role="group" aria-label="可用思考档位">{levels.map(option => <button type="button"
        key={option.value} aria-label={option.label} aria-pressed={option.value === current} disabled={pending || disabled || reasoningDisabled}
        onClick={() => void save(option.value)}>{optionName(option)}</button>)}</div>
      </>}
      {value === 'configured' && <p className="reasoning-note">原始配置尚未校验；选择档位后使用已校验的设置。</p>}
      {index < 0 && value === 'provider_default' && <p className="reasoning-note">供应商未公开默认档位，当前不发送推理参数。</p>}
      {!pendingModel && <details className="reasoning-help"><summary>默认与生效方式</summary><p className="reasoning-note">{description}</p></details>}
      </section>
      </>}
      <p className="model-settings-notice" role="status">{pending ? '正在切换…' : modelNotice}</p>
      {error && <p role="alert" className="choice-menu-error">{error}</p>}
    </div>, document.body)}
  </div>
}
