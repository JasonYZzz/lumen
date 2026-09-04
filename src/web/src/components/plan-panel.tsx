'use client'

import { CaretDown, CheckCircle, Circle, CircleNotch, ListChecks, MinusCircle, WarningCircle, X } from '@phosphor-icons/react'
import { useEffect, useId, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import type { PlanState, PlanStep } from '@/lib/api/types'
import { planPresentation, stepStatusLabel } from '../lib/plan-presentation'
import { useAnchoredPopover } from './use-anchored-popover'

/** Live read-only progress; opening this never approves or changes the plan. */
export function PlanProgress({ plan, running, stopping = false }: {
  plan: PlanState; running: boolean; stopping?: boolean
}) {
  const [open, setOpen] = useState(false)
  const trigger = useRef<HTMLButtonElement>(null)
  const popover = useRef<HTMLDivElement>(null)
  const id = useId()
  const view = planPresentation(plan)
  const position = useAnchoredPopover({ open, anchor: trigger.current, popoverRef: popover, width: 390, placement: 'above' })
  useEffect(() => {
    if (!open) return
    popover.current?.focus({ preventScroll: true })
    const outside = (event: PointerEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !popover.current?.contains(event.target as Node)) setOpen(false)
    }
    const escape = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || event.isComposing || event.keyCode === 229) return
      event.preventDefault()
      setOpen(false)
      trigger.current?.focus({ preventScroll: true })
    }
    const focusOutside = (event: FocusEvent) => {
      if (!trigger.current?.contains(event.target as Node) && !popover.current?.contains(event.target as Node)) setOpen(false)
    }
    window.addEventListener('pointerdown', outside, true)
    window.addEventListener('keydown', escape)
    window.addEventListener('focusin', focusOutside)
    return () => {
      window.removeEventListener('pointerdown', outside, true)
      window.removeEventListener('keydown', escape)
      window.removeEventListener('focusin', focusOutside)
    }
  }, [open])
  const currentIndex = plan.steps.findIndex((step) => step.id === view.current?.id)
  const label = stopping ? '正在停止…' : view.status === '受阻' || view.settled || plan.lifecycle === 'cancelled'
    ? view.status : view.current?.title ?? view.status
  return <div className="plan-progress">
    <button ref={trigger} type="button" className="plan-progress-pill" aria-label="查看任务进度"
      aria-expanded={open} aria-controls={open ? id : undefined} aria-haspopup="dialog" onClick={() => setOpen(!open)}>
      {running && !view.settled && view.status !== '受阻'
        ? <CircleNotch size={16} className="spin" aria-hidden="true" /> : <ListChecks size={16} aria-hidden="true" />}
      <span className="plan-progress-count">{currentIndex >= 0 && !view.settled ? `步骤 ${currentIndex + 1} / ${plan.steps.length}` : `${view.completed} / ${plan.steps.length}`}</span>
      <span className="plan-progress-title">{label}</span>
      <CaretDown size={14} className={open ? 'is-expanded' : ''} aria-hidden="true" />
    </button>
    {open && createPortal(<div ref={popover} id={id} role="dialog" aria-label="任务进度" tabIndex={-1}
      className="plan-progress-popover" style={position}>
      <header><strong>任务进度</strong><span>已完成 {view.completed} / {plan.steps.length}</span>
        <button type="button" aria-label="关闭任务进度" onClick={() => { setOpen(false); trigger.current?.focus() }}><X size={16} /></button>
      </header>
      <PlanPanel plan={plan} running={running} />
    </div>, document.body)}
  </div>
}

export function PlanProposal({ plan }: { plan: PlanState }) {
  return <section className="plan-proposal" aria-label="待确认方案">
    <header><h3>建议方案</h3><span>版本 {plan.revision}</span></header>
    <PlanPanel plan={plan} />
    <p className="plan-proposal-note">确认后开始执行；你也可以先提出修改意见。</p>
  </section>
}

export function PlanReview({ revision, waiting, busy, disabled, feedback, onFeedback, onReview }: {
  revision: number; waiting: boolean; busy: boolean; disabled: boolean; feedback: string
  onFeedback: (value: string) => void; onReview: (action: 'approve' | 'reject') => void
}) {
  const [editing, setEditing] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  useEffect(() => { if (editing) input.current?.focus() }, [editing])
  return <section className="plan-review" aria-label="确认方案" aria-busy={busy}>
    <div className="plan-review-heading"><strong>{editing ? '你希望怎样调整？' : waiting ? '方案已确认，继续执行？' : '按这个方案执行？'}</strong>
      <span>版本 {revision} · {editing ? '发送意见后，Lumen 会重新规划。' : '执行仍遵守当前工具审批设置。'}</span></div>
    {editing ? <>
      <textarea ref={input} value={feedback} disabled={busy} placeholder="告诉 Lumen 需要调整什么…"
        aria-label="计划修改意见" onChange={(event) => onFeedback(event.target.value)} />
      <div className="plan-review-actions">
        <button type="button" disabled={busy || disabled || !feedback.trim()} onClick={() => onReview('reject')}>{busy ? '正在提交…' : '发送意见并重新规划'}</button>
        <button type="button" className="quiet" disabled={busy} onClick={() => setEditing(false)}>返回确认</button>
      </div>
    </> : <div className="plan-review-options">
      <button type="button" disabled={busy || disabled} onClick={() => onReview('approve')}>{busy ? '正在提交…' : waiting ? '继续执行' : '确认并开始执行'}</button>
      <button type="button" className="quiet" disabled={busy || disabled} onClick={() => setEditing(true)}>先调整方案</button>
    </div>}
  </section>
}

export function PlanPanel({ plan, running = false }: {
  plan: PlanState
  running?: boolean
}) {
  const view = planPresentation(plan)
  return (
    <section className="web-plan is-detailed" aria-label="任务计划">
        {plan.goal && <p className="plan-goal">{plan.goal}</p>}
        <ol>
          {plan.steps.map((step) => <li key={step.id} className={`is-${step.status}`} aria-current={step.id === view.current?.id ? 'step' : undefined}>
            <PlanStepIcon status={step.status} running={running} />
            <div className="plan-step-body">
              <div className="plan-step-heading"><span>{step.title}</span><small>{stepStatusLabel[step.status]}</small></div>
              {(step.note || step.depends_on.length > 0 || step.acceptance_criteria.length > 0) && <details className="plan-step-details">
                <summary>查看说明{step.acceptance_criteria.length ? '与验收条件' : ''}</summary>
                {step.note && <p>{step.note}</p>}
                {step.depends_on.length > 0 && <p>依赖：{step.depends_on.map((id) => plan.steps.find((item) => item.id === id)?.title ?? id).join('、')}</p>}
                {step.acceptance_criteria.map((criterion) => <p key={criterion.id}>验收条件：{criterion.description}</p>)}
              </details>}
            </div>
          </li>)}
        </ol>
        {!plan.steps.length && <p className="plan-empty">尚未生成步骤</p>}
    </section>
  )
}

function PlanStepIcon({ status, running }: { status: PlanStep['status']; running: boolean }) {
  if (status === 'completed') return <CheckCircle size={18} weight="fill" aria-hidden="true" />
  if (status === 'in_progress') return <CircleNotch size={18} className={running ? 'spin' : ''} aria-hidden="true" />
  if (status === 'blocked') return <WarningCircle size={18} weight="fill" aria-hidden="true" />
  if (status === 'skipped') return <MinusCircle size={18} aria-hidden="true" />
  return <Circle size={18} aria-hidden="true" />
}
