'use client'

import { useEffect, useState } from 'react'
import { createPortal } from 'react-dom'
import { X } from '@phosphor-icons/react'
import { lumenApi } from '../lib/api/client'
import type { AttachmentRef } from '../lib/api/types'
import { useModalFocus } from './use-modal-focus'

/** Both drafts and journal messages read the canonical attachment through the Host. */
export function AttachmentImage({ attachment }: { attachment: AttachmentRef }) {
  const [url, setUrl] = useState('')
  const [error, setError] = useState(false)
  const [attempt, setAttempt] = useState(0)
  const [open, setOpen] = useState(false)
  const modal = useModalFocus(() => setOpen(false), open)
  const { artifactRef, filename, mediaType, byteSize, kind } = attachment
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    setUrl('')
    setError(false)
    void lumenApi.readAttachment({ artifactRef, filename, mediaType, byteSize, kind }, controller.signal)
      .then((blob) => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(blob)
        setUrl(objectUrl)
      }).catch(() => { if (!controller.signal.aborted) setError(true) })
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [artifactRef, filename, mediaType, byteSize, kind, attempt])
  return <>
    <button type="button" className="attachment-thumbnail" aria-label={`${error ? '重试加载' : '查看图片'} ${filename}`}
      disabled={!url && !error} onClick={() => error ? setAttempt(attempt + 1) : setOpen(true)}>
      {url && !error ? <img src={url} alt={filename} onError={() => setError(true)} />
        : <span>{error ? '加载失败，点击重试' : '正在加载图片…'}</span>}
    </button>
    {open && url && createPortal(<div className="attachment-lightbox" onClick={() => setOpen(false)}>
      <div ref={modal} role="dialog" aria-modal="true" aria-label={filename} tabIndex={-1}
        onClick={(event) => event.stopPropagation()}>
        <button type="button" aria-label="关闭图片预览" onClick={() => setOpen(false)}><X size={24} /></button>
        <img src={url} alt={filename} />
        <p>{filename}</p>
      </div>
    </div>, document.body)}
  </>
}
