'use client'

import { ArrowUpRight, CaretDown, X } from '@phosphor-icons/react'
import { useRef, useState } from 'react'
import type { AssistantIdea } from '@/lib/api/types'

export function IdeaWorkbench({idea, disabled, onStart, onOpen, onDismiss}: {
  idea: AssistantIdea; disabled: boolean
  onStart?: (idea: AssistantIdea, prompt: string) => Promise<void>
  onOpen: () => void; onDismiss: () => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [scope, setScope] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const isTask = (idea.destination ?? 'task') === 'task'
  const canStart = !idea.input_hint || Boolean(scope.trim())
  const launch = async () => {
    if (!onStart || pending.current || disabled || !canStart) return
    pending.current = true; setBusy(true); setError('')
    const prompt = [idea.intent, scope.trim() ? `用户指定的任务范围与要求：\n${scope.trim()}` : '',
      idea.outcome ? `预期成果：${idea.outcome}` : '',
      '请实际完成以上任务。只读取范围内的材料，缺少关键输入时询问；遵守当前审批和沙箱，不擅自创建定时任务。',
    ].filter(Boolean).join('\n\n')
    try { await onStart(idea, prompt) }
    catch (error) { setError(error instanceof Error ? error.message : '暂时无法开始，请重试') }
    finally { pending.current = false; setBusy(false) }
  }
  return <article className={`idea-workbench ${expanded ? 'is-expanded' : ''}`}>
    <div className="idea-summary">
      <div>
        <h3>{idea.title}</h3><p>{idea.reason}</p>
        <div className="idea-context"><span>基于</span><strong>{idea.source_label || '通用起点'}</strong></div>
        {idea.outcome && <div className="idea-outcome"><span>你会得到</span><p>{idea.outcome}</p></div>}
      </div>
      <div className="idea-summary-actions">
        {isTask ? <button type="button" aria-expanded={expanded} disabled={busy} onClick={() => setExpanded(!expanded)}>
          {expanded ? '收起方案' : '查看方案'}<CaretDown size={14} /></button>
          : <button type="button" onClick={onOpen}>{idea.destination === 'goal' ? '查看待验收目标' : '检查跟进安排'}<ArrowUpRight size={15} /></button>}
        <button type="button" className="idea-dismiss" disabled={disabled || busy} onClick={onDismiss}
          aria-label={idea.dismissed ? `恢复建议：${idea.title}` : `忽略建议：${idea.title}`} title={idea.dismissed ? '恢复建议' : '忽略建议'}>
          {idea.dismissed ? '恢复' : <X size={16} />}</button>
      </div>
    </div>
    {expanded && <form className="idea-plan" onSubmit={event => {event.preventDefault(); void launch()}}>
      {!!idea.steps?.length && <ol>{idea.steps.map(step => <li key={step}>{step}</li>)}</ol>}
      <label>{idea.input_hint ? '先确定任务范围' : '补充要求（可选）'}
        <textarea value={scope} placeholder={idea.input_hint || '例如：只核对现有资料，先处理最重要的一项'}
          maxLength={8000} required={Boolean(idea.input_hint)} disabled={busy} onChange={event => setScope(event.target.value)} />
      </label>
      <div className="idea-launch"><p>点击后立即开始一项任务，使用当前模型与权限。成果文件自动收录。</p>
        <button className="assistant-primary" type="submit" disabled={disabled || busy || !canStart || !onStart}>
          {busy ? '正在开始…' : '开始任务'}<ArrowUpRight size={16} /></button></div>
      {error && <p role="alert" className="assistant-error">{error}</p>}
    </form>}
  </article>
}
