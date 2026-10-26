'use client'

import { useEffect, useMemo, useState } from 'react'
import { CaretLeft, CaretRight } from '@phosphor-icons/react'
import { AshenPress, ashenPressSource } from '@designcodeio/threeui'
import '@designcodeio/threeui/style.css'
import { librarySource, type LibraryTheme } from '../shaders/ashen-press/library-source'
import type { AssistantResource } from '@/lib/api/types'
import { resourceTypeLabel } from './resource-icon'

export function LibraryScene({ resources, onOpen }: { resources: AssistantResource[]; onOpen: (id: string) => void }) {
  const [page, setPage] = useState(0)
  const [theme, setTheme] = useState<LibraryTheme>({page:'#faf7f8',surface:'#ffffff',accent:'#795271',ink:'#302b33',line:'#e6dce4'})
  useEffect(() => {
    const update = () => {
      const css = getComputedStyle(document.documentElement)
      setTheme(current => Object.fromEntries(Object.entries(current).map(([key, fallback]) =>
        [key, css.getPropertyValue(`--${key}`).trim() || fallback])) as LibraryTheme)
    }
    update()
    const observer = new MutationObserver(update)
    observer.observe(document.documentElement, {attributes:true,attributeFilter:['data-appearance']})
    return () => observer.disconnect()
  }, [])
  const currentPage = Math.min(page, Math.max(0, Math.ceil(resources.length / 10) - 1))
  const visible = useMemo(() => resources.slice(currentPage * 10, (currentPage + 1) * 10), [resources, currentPage])
  const sceneDocument = useMemo(() => librarySource(ashenPressSource, visible.map(r => ({
    id:r.id, title:r.title, type:resourceTypeLabel(r), revision:r.revision,
    date:new Date(r.updated_at || r.created_at).toLocaleDateString('zh-CN'),
    source:r.status !== 'active' ? '来源待核实' : r.session_id ? '来自对话' : '来自资料空间',
  })), theme), [visible, theme])
  return <div className="library-scene">
    <div className="library-scene-frame">
      <AshenPress source={sceneDocument} style={{background:theme.page}} onOpenResource={id => {
        if (visible.some(r => r.id === id && !r.deleted_at)) onOpen(id)
      }} />
    </div>
    <div className="library-scene-caption">
      <p>取出一本资料，轻点翻转或拖拽旋转；打开资料可阅读全文。</p>
      {resources.length > 10 && <div>
        <button aria-label="上一层资料" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}><CaretLeft size={16} /></button>
        <span>{currentPage * 10 + 1}–{Math.min((currentPage + 1) * 10, resources.length)} / {resources.length}</span>
        <button aria-label="下一层资料" disabled={(currentPage + 1) * 10 >= resources.length} onClick={() => setPage(currentPage + 1)}><CaretRight size={16} /></button>
      </div>}
    </div>
  </div>
}
