'use client'

import { Check, Copy, WarningCircle } from '@phosphor-icons/react'
import { useEffect, useRef, useState } from 'react'

export function CopyButton({ text, label = '复制回复', className = 'copy-button', showLabel = false }: {
  text: string
  label?: string
  className?: string
  showLabel?: boolean
}) {
  const [status, setStatus] = useState<'idle' | 'copied' | 'failed'>('idle')
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (timer.current) clearTimeout(timer.current) }, [])
  const title = status === 'copied' ? '已复制' : status === 'failed' ? '复制失败，请重试' : label
  return <button className={className} type="button" aria-label={title} title={title} onClick={async () => {
    try {
      await navigator.clipboard.writeText(text)
      setStatus('copied')
    } catch {
      setStatus('failed')
    }
    if (timer.current) clearTimeout(timer.current)
    timer.current = setTimeout(() => setStatus('idle'), 1800)
  }}>
    {status === 'copied' ? <Check size={14} aria-hidden="true" /> : status === 'failed' ? <WarningCircle size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
    {showLabel && <span>{title}</span>}
    <span className="sr-only" role="status">{status !== 'idle' ? title : ''}</span>
  </button>
}
