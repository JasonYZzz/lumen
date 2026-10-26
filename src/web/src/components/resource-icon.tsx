import { useEffect, useRef, useState } from 'react'
import { File, FileCode, FileText, Globe, Image, PresentationChart, Table } from '@phosphor-icons/react'
import type { AssistantResource } from '@/lib/api/types'
import { lumenApi } from '@/lib/api/client'

export function resourceTypeLabel(resource: AssistantResource) {
  return resource.file_type ? ({document: '文档', site: '网页', image: '图像', spreadsheet: '表格',
    presentation: '演示文稿', code: '代码', file: '文件'}[resource.file_type])
    : resource.kind === 'note' ? '页面' : resource.kind === 'trial' ? '试跑预览' : '研究报告'
}

export function ResourceIcon({ type, size = 21 }: {type?: AssistantResource['file_type']; size?: number}) {
  const Icon = type ? ({document: FileText, site: Globe, image: Image, spreadsheet: Table,
    presentation: PresentationChart, code: FileCode, file: File}[type]) : FileText
  return <Icon size={size} aria-hidden="true" />
}

export function ResourceThumbnail({ resource }: {resource: AssistantResource}) {
  const [url, setUrl] = useState<string | null>(null)
  const [excerpt, setExcerpt] = useState('')
  const [visible, setVisible] = useState(false)
  const ref = useRef<HTMLSpanElement>(null)
  useEffect(() => {
    if (!ref.current || typeof IntersectionObserver === 'undefined') {setVisible(true); return}
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {setVisible(true); observer.disconnect()}
    }, {rootMargin: '120px'})
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [])
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl: string | undefined
    setUrl(null)
    setExcerpt('')
    if (!visible || resource.status !== 'active' || resource.deleted_at) return
    if (resource.file_type === 'image') {
      void lumenApi.readResourceFile(resource.id, resource.revision, controller.signal).then(blob => {
        if (controller.signal.aborted) return
        objectUrl = URL.createObjectURL(resource.file_path?.endsWith('.svg') ? new Blob([blob], {type: 'image/svg+xml'}) : blob)
        setUrl(objectUrl)
      }).catch(() => { /* Keep the file icon when a thumbnail is unavailable. */ })
    } else if (!resource.file_path || /\.(md|markdown|txt)$/i.test(resource.file_path)) {
      void (async () => {
        try {
          const value = await lumenApi.assistantResource(resource.id, false, resource.revision)
          if (!controller.signal.aborted) setExcerpt(value.content.slice(0, 600).replace(/!\[[^\]]*\]\([^)]*\)/g, '').replace(/[#*`>]/g, '').trim())
        } catch { /* A type cover remains usable when content is unavailable. */ }
      })()
    }
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [visible, resource.id, resource.revision, resource.file_type, resource.file_path, resource.status, resource.deleted_at])
  return <span ref={ref} className={`library-thumbnail type-${resource.file_type ?? 'page'} ${excerpt ? 'has-excerpt' : ''}`}>
    {url ? <img src={url} alt="" onError={() => setUrl(null)} /> : excerpt
      ? <span className="library-excerpt"><strong>{resource.title}</strong><span>{excerpt}</span></span>
      : <span className="library-type-cover"><ResourceIcon type={resource.file_type} size={36} /><span>{resourceTypeLabel(resource)}</span><strong>{resource.title}</strong></span>}
  </span>
}
