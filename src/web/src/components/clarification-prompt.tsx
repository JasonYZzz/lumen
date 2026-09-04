'use client'

import { useId, useRef, useState } from 'react'
import type { SessionSnapshot } from '@/lib/api/types'

export function ClarificationPrompt({ question, onAnswer, disabled = false }: {
  question: NonNullable<SessionSnapshot['pendingClarification']>
  onAnswer: (answer: string) => Promise<void>
  disabled?: boolean
}) {
  const heading = useId()
  const errorId = useId()
  const [answer, setAnswer] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')
  const pending = useRef(false)
  const send = async (text: string) => {
    if (!text.trim() || pending.current || disabled) return
    pending.current = true
    setSubmitting(true)
    setError('')
    try {
      await onAnswer(text.trim())
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '回答未发送，请重试。')
    } finally {
      pending.current = false
      setSubmitting(false)
    }
  }
  return (
    <section className="clarification-prompt" aria-labelledby={heading} aria-busy={submitting}>
      <span className="clarification-eyebrow">需要你选择</span>
      <h3 id={heading}>{question.question}</h3>
      {question.choices.length > 0 && (
        <div className="clarification-options">
          {question.choices.map((choice, index) => (
            <button type="button" key={`${index}:${choice}`} disabled={disabled || submitting}
              onClick={() => void send(choice)}>
              <span aria-hidden="true">{index + 1}</span>{choice}
            </button>
          ))}
        </div>
      )}
      <form onSubmit={(event) => { event.preventDefault(); void send(answer) }}>
        <label htmlFor={`${heading}-answer`}>{question.choices.length ? '或输入其他回答' : '你的回答'}</label>
        <div className="clarification-answer">
          <input id={`${heading}-answer`} value={answer} onChange={(event) => setAnswer(event.target.value)}
            disabled={disabled || submitting} aria-describedby={error ? errorId : undefined}
            placeholder="补充你的想法…" maxLength={4000} />
          <button type="submit" disabled={disabled || submitting || !answer.trim()}>
            {submitting ? '发送中…' : '发送回答'}
          </button>
        </div>
      </form>
      {error && <p className="clarification-error" role="alert" id={errorId}>{error}</p>}
    </section>
  )
}
