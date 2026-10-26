'use client'

import dynamic from 'next/dynamic'
import {
  BookmarkSimple,
  ChatCircle,
  Clock,
  FileText,
  Lightbulb,
  Newspaper,
  Plus,
  Target,
  X,
  CaretDown,
  MagnifyingGlass,
  SquaresFour,
  Books,
  ListBullets,
  ArrowClockwise,
  DotsThree,
  PencilSimpleLine,
  Trash,
  ArrowCounterClockwise,
  ArrowUpRight,
  CheckCircle,
  WarningCircle,
} from '@phosphor-icons/react'
import {
  useCallback,
  useEffect,
  useId,
  useState,
  useRef,
  type FormEvent,
} from 'react'
import { lumenApi } from '@/lib/api/client'
import type {
  AssistantGoal,
  AssistantIdea,
  AssistantContextRef,
  AssistantGoalDetail,
  AssistantState,
  AssistantResource,
  AutomationOccurrence,
  AssistantFeedPage,
  AutomationAction,
  AutomationDefinition,
  AutomationSpec,
} from '@/lib/api/types'
import { MarkdownMessage } from './markdown-message'
import { foxImage } from './mascot/fox-growth'
import { DocumentPreview } from './document-preview'
import { ResourceIcon, ResourceThumbnail, resourceTypeLabel } from './resource-icon'
import { useModalFocus } from './use-modal-focus'
import { ActionsMenu } from './session-actions-menu'
import { IdeaWorkbench } from './idea-workbench'
import './assistant-workspace.css'

const LibraryScene = dynamic(() => import('./library-scene').then(module => module.LibraryScene), {ssr:false})

export type AssistantPage =
  | 'chat'
  | 'feed'
  | 'ideas'
  | 'goals'
  | 'library'
  | 'automations'
export const assistantPages = [
  { id: 'chat', label: '对话', icon: ChatCircle },
  { id: 'feed', label: '信息流', icon: Newspaper },
  { id: 'ideas', label: '灵感', icon: Lightbulb },
  { id: 'goals', label: '目标', icon: Target },
  { id: 'library', label: '资料空间', icon: FileText },
  { id: 'automations', label: '活动与持续跟进', icon: Clock },
] as const

export type AssistantDiscussion = { intent: string; context?: AssistantContextRef }
export const goalStatusLabels = {active: '未完成', completed: '已完成', archived: '已归档'}
export const goalExecutionLabels = {active: '正在推进', paused: '已暂停', blocked: '需要处理', budget_reached: '已达轮数上限', review: '待你验收', accepted: '已验收'}
export type AssistantView = { filter: string; query: string; scrollTop: number }
type ManagedItem = {kind: 'goal'; value: AssistantGoal} | {kind: 'resource'; value: AssistantResource}
  | {kind: 'collection'; value: AssistantState['collections'][number]}

const libraryTypes = [
  ['all', '全部'], ['document', '文档'], ['site', '网页'], ['image', '图像'],
  ['spreadsheet', '表格'], ['presentation', '演示文稿'], ['note', '页面'],
] as const

const resultLabels: Record<string, string> = {
  complete: '已完成',
  partial: '部分来源未读取',
  no_change: '暂无变化',
  needs_user_action: '需要你处理',
  failed: '执行失败',
  cancelled: '已停止',
  interrupted: '执行中断',
  budget_reached: '达到执行预算',
  skipped: '已跳过',
}
const date = (value: string) =>
  new Date(value).toLocaleString('zh-CN', {
    month: 'numeric',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
  })

export function AssistantWorkspace({
  page,
  initialIntent,
  onDiscuss,
  onOpenSession,
  objectId,
  onNavigate,
  viewState,
  sourceSessionId,
  onGoalCreated,
  onStartIdea,
  onPlanGoal,
}: {
  page: Exclude<AssistantPage, 'chat'>
  initialIntent: string
  onDiscuss: (discussion: AssistantDiscussion) => void
  onOpenSession: (id: string) => void
  objectId?: string
  onNavigate?: (page: AssistantPage, id?: string) => void
  viewState?: Map<string, AssistantView>
  sourceSessionId?: string
  onGoalCreated?: () => void
  onStartIdea?: (idea: AssistantIdea, prompt: string) => Promise<void>
  onPlanGoal?: (goalId: string, requestId: string) => Promise<void>
}) {
  const localViews = useRef(new Map<string, AssistantView>())
  const views = viewState ?? localViews.current
  const [state, setState] = useState<AssistantState | null>(null)
  const [error, setError] = useState('')
  const [readError, setReadError] = useState('')
  const [busy, setBusy] = useState(false)
  const mutationPending = useRef(false)
  const actionRequests = useRef(new Map<string, string>())
  const mutationFocus = useRef<HTMLElement | null>(null)
  const [filter, setFilter] = useState(views.get(page)?.filter ?? 'all')
  const [query, setQuery] = useState(views.get(page)?.query ?? '')
  const viewRef = useRef<AssistantView>({filter, query, scrollTop: 0})
  viewRef.current.filter = filter; viewRef.current.query = query
  const pendingScroll = useRef<number | null>(views.get(page)?.scrollTop ?? null)
  const [editor, setEditor] = useState<AutomationDefinition | 'new' | null>(
    null,
  )
  const [creatingGoal, setCreatingGoal] = useState(false)
  const [planning, setPlanning] = useState(false)
  const [feedExpanded, setFeedExpanded] = useState(true)
  const [planningFailure, setPlanningFailure] = useState<{id: string; requestId: string; message: string} | null>(null)
  const planningRequest = useRef(false)
  useEffect(() => {
    if (!window.matchMedia) return
    const wide = window.matchMedia('(min-width: 1051px)')
    const update = () => setFeedExpanded(wide.matches)
    update()
    wide.addEventListener('change', update)
    return () => wide.removeEventListener('change', update)
  }, [])
  const [creatingNote, setCreatingNote] = useState(false)
  const [goalDetail, setGoalDetail] = useState<AssistantGoalDetail | null>(null)
  const [goalLoading, setGoalLoading] = useState(false)
  const [editingGoal, setEditingGoal] = useState<AssistantGoal | null>(null)
  const [startingGoal, setStartingGoal] = useState<AssistantGoal | null>(null)
  const [closingGoal, setClosingGoal] = useState<{goal: AssistantGoal; status: 'completed' | 'archived'} | null>(null)
  const [followGoal, setFollowGoal] = useState<AssistantGoal | null>(null)
  const [collectionTitle, setCollectionTitle] = useState('')
  const collectionIdentity = useRef(crypto.randomUUID())
  const [creatingCollection, setCreatingCollection] = useState(false)
  const [menu, setMenu] = useState<{anchor: HTMLElement; item: ManagedItem} | null>(null)
  const [renaming, setRenaming] = useState<ManagedItem | null>(null)
  const [renameTitle, setRenameTitle] = useState('')
  const [removing, setRemoving] = useState<ManagedItem | null>(null)
  const [grid, setGrid] = useState(true)
  const [shelf, setShelf] = useState(true)
  const showShelf = shelf && filter !== 'trash'
  const importRef = useRef<HTMLInputElement>(null)
  const [resource, setResource] = useState<Awaited<
    ReturnType<typeof lumenApi.assistantResource>
  > | null>(null)
  const [editingResource, setEditingResource] = useState<typeof resource>(null)
  const [inspecting, setInspecting] = useState<AutomationOccurrence | null>(
    null,
  )
  const [libraryItems, setLibraryItems] = useState<AssistantResource[]>([])
  const [fileDetails, setFileDetails] = useState(false)
  const [feedItems, setFeedItems] = useState<AssistantFeedPage['items']>([])
  const [cursor, setCursor] = useState<number | null>(null)
  const [collectionBusy, setCollectionBusy] = useState(false)
  const [collectionError, setCollectionError] = useState('')
  const [collectionEpoch, setCollectionEpoch] = useState(0)
  const detailRequest = useRef(0)
  const openedAutomation = useRef<string | null>(null)
  const collectionRequest = useRef(0)
  const sectionRef = useRef<HTMLElement>(null)
  const restoreFocus = useRef(false)
  const signature =
    page === 'feed'
      ? JSON.stringify([
          state?.feed.map((item) => [item.id, item.read]),
          state?.resources.map((item) => [item.id, item.saved]),
        ])
      : JSON.stringify(
          state?.resources.map((item) => [item.id, item.revision, item.saved]),
        )
  useEffect(() => {
    if (page !== 'feed' && page !== 'library') return
    const request = ++collectionRequest.current
    setCollectionBusy(true)
    const timer = window.setTimeout(() => {
      const read =
        page === 'library'
          ? lumenApi.assistantResources(query, filter).then((value) => {
              if (request === collectionRequest.current) {
                setLibraryItems(value.items)
                setCursor(value.next_cursor)
              }
            })
          : lumenApi.assistantFeed(filter).then((value) => {
              if (request === collectionRequest.current) {
                setFeedItems(value.items)
                setCursor(value.next_cursor)
              }
            })
      void read
        .then(() => {
          if (request === collectionRequest.current) setCollectionError('')
        })
        .catch((reason) => {
          if (request === collectionRequest.current)
            setCollectionError(
              reason instanceof Error ? reason.message : '无法读取记录',
            )
        })
        .finally(() => {
          if (request === collectionRequest.current) setCollectionBusy(false)
        })
    }, 180)
    restoreFocus.current = Boolean(
      sectionRef.current?.contains(document.activeElement),
    )
    return () => {
      window.clearTimeout(timer)
      collectionRequest.current++
    }
  }, [page, query, filter, signature, collectionEpoch])
  useEffect(() => {
    if (pendingScroll.current !== null && sectionRef.current && !collectionBusy) {
      sectionRef.current.scrollTop = pendingScroll.current
      pendingScroll.current = null
    }
    if (restoreFocus.current && document.activeElement === document.body)
      sectionRef.current?.focus({ preventScroll: true })
    restoreFocus.current = false
  }, [libraryItems, feedItems, collectionBusy])
  const more = async () => {
    if (!cursor) return
    const request = collectionRequest.current
    setCollectionBusy(true)
    try {
      if (page === 'library') {
        const value = await lumenApi.assistantResources(query, filter, cursor)
        if (request === collectionRequest.current) {
          setLibraryItems((items) => [...items, ...value.items])
          setCursor(value.next_cursor)
        }
      } else {
        const value = await lumenApi.assistantFeed(filter, cursor)
        if (request === collectionRequest.current) {
          setFeedItems((items) => [...items, ...value.items])
          setCursor(value.next_cursor)
        }
      }
    } catch (reason) {
      if (request === collectionRequest.current)
        setCollectionError(
          reason instanceof Error ? reason.message : '无法读取更多记录',
        )
    } finally {
      if (request === collectionRequest.current) setCollectionBusy(false)
    }
  }
  const load = useCallback(async () => {
    const next = await lumenApi.assistant()
    setState(next)
    setError('')
  }, [])
  useEffect(() => {
    if (page !== 'automations') { openedAutomation.current = null; return }
    if (!objectId || openedAutomation.current === objectId) return
    const definition = state?.definitions.find(item => item.id === objectId)
    if (definition) { openedAutomation.current = objectId; setEditor(definition) }
  }, [page, objectId, state?.definitions])
  useEffect(() => {
    let active = true
    const refresh = () =>
      lumenApi
        .assistant()
        .then((value) => {
          if (active) {
            setState(value)
            setReadError('')
          }
        })
        .catch((reason) => {
          if (active)
            setReadError(
              reason instanceof Error ? reason.message : '无法读取助理状态',
            )
        })
    void refresh()
    const timer = window.setInterval(() => {
      if (!document.hidden) void refresh()
    }, 3000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [])
  useEffect(() => {
    const saved = views.get(page)
    setFilter(saved?.filter ?? 'all'); setQuery(saved?.query ?? '')
    pendingScroll.current = saved?.scrollTop ?? 0
    setInspecting(null)
    setResource(null)
    detailRequest.current++
    return () => {
      views.set(page, {...viewRef.current})
      detailRequest.current++
    }
  }, [page, views])
  useEffect(() => {
    if (page === 'automations' && initialIntent) setEditor('new')
    if (page === 'goals' && (initialIntent || sourceSessionId)) setCreatingGoal(true)
  }, [page, initialIntent, sourceSessionId])
  const openGoal = useCallback(async (id: string) => {
    const request = ++detailRequest.current
    setGoalLoading(true)
    setGoalDetail(current => current?.goal.id === id ? current : null)
    setError('')
    try {
      const next = await lumenApi.assistantGoal(id)
      if (request === detailRequest.current) setGoalDetail(next)
    } catch (reason) {
      if (request === detailRequest.current) {
        setGoalDetail(null)
        setError(reason instanceof Error ? reason.message : '目标暂不可用')
      }
    } finally {
      if (request === detailRequest.current) setGoalLoading(false)
    }
  }, [])
  useEffect(() => {
    if (page === 'goals' && objectId) void openGoal(objectId)
    else {
      setGoalDetail(null)
      setGoalLoading(false)
      if (page === 'library' && objectId) void openResource(objectId)
    }
    return () => { detailRequest.current++ }
    // The route is the object identity; polling must not reopen an editor.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [page, objectId])
  const visitGoal = (id: string) => {
    if (onNavigate) onNavigate('goals', id)
    else void openGoal(id)
  }
  const selectedGoalId = page === 'goals' ? objectId ?? goalDetail?.goal.id : undefined
  useEffect(() => {
    if (!selectedGoalId || !state) return
    if (state.goals.find(goal => goal.id === selectedGoalId)?.deleted_at) {
      detailRequest.current++
      setGoalDetail(null)
      setGoalLoading(false)
    }
    else void openGoal(selectedGoalId)
  }, [selectedGoalId, state, openGoal])
  const mutate = async (operation: () => Promise<unknown>) => {
    if (mutationPending.current) return false
    mutationPending.current = true
    mutationFocus.current =
      document.activeElement instanceof HTMLElement
        ? document.activeElement
        : null
    setBusy(true)
    setError('')
    try {
      await operation()
      await load()
      if (goalDetail && !(removing?.kind === 'goal' && removing.value.id === goalDetail.goal.id)) await openGoal(goalDetail.goal.id)
      setCollectionEpoch((value) => value + 1)
      return true
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '保存失败，请重试')
      return false
    } finally {
      mutationPending.current = false
      setBusy(false)
    }
  }
  useEffect(() => {
    if (
      !busy &&
      document.activeElement === document.body &&
      mutationFocus.current?.isConnected
    ) {
      mutationFocus.current.focus({ preventScroll: true })
    }
    if (!busy) mutationFocus.current = null
  }, [busy])
  const action = (
    definition: AutomationDefinition,
    kind: AutomationAction['action'],
  ) => {
    const key = JSON.stringify([definition.id, definition.revision, kind])
    const requestId = actionRequests.current.get(key) ?? crypto.randomUUID()
    actionRequests.current.set(key, requestId)
    void mutate(() =>
      lumenApi.automation({
        action: kind,
        id: definition.id,
        expected_revision: definition.revision,
        request_id: requestId,
      }),
    ).then((saved) => {
      if (saved) actionRequests.current.delete(key)
    })
  }
  async function openResource(id: string, historical = false, revision?: number) {
    const request = ++detailRequest.current
    setError('')
    try {
      const value = await lumenApi.assistantResource(id, historical, revision)
      if (request !== detailRequest.current) return
      setResource(value)
      setFileDetails(false)
      setInspecting(null)
      if (!historical && !revision) onNavigate?.('library', id)
    } catch (reason) {
      if (request === detailRequest.current)
        setError(reason instanceof Error ? reason.message : '资源暂不可用')
    }
  }
  const importFile = async (file: File) => {
    if (file.size > 20 * 1024 * 1024) {
      setError('文件超过 20 MiB 上限，请选择更小的文件。')
      return
    }
    await mutate(async () => {
      await lumenApi.importResourceFile(file, crypto.randomUUID())
    })
  }
  const inspect = async (id: string) => {
    const request = ++detailRequest.current
    setError('')
    try {
      const value = await lumenApi.assistantOccurrence(id)
      if (request === detailRequest.current) setInspecting(value)
    } catch (reason) {
      if (request === detailRequest.current)
        setError(reason instanceof Error ? reason.message : '执行记录暂不可用')
    }
  }
  const title = assistantPages.find((item) => item.id === page)?.label
  const ownerDisabled =
    busy || planning || !state?.owner || state.status === 'storage_error'
  const updateGoal = (goal: AssistantGoal, changes: Partial<Pick<AssistantGoal, 'status' | 'milestones'>>) => mutate(() => lumenApi.editAssistant({
    action: 'goal', id: goal.id, expected_revision: goal.revision,
    goal: {title: goal.title, intent: goal.intent, status: goal.status, milestones: goal.milestones, automation_ids: goal.automation_ids, ...changes},
  }))
  const requestGoalStatus = (goal: AssistantGoal, status: AssistantGoal['status']) => {
    if (status !== 'active' && state?.definitions.some(d => goal.automation_ids.includes(d.id) && d.enabled)) {
      setClosingGoal({goal, status})
    } else void updateGoal(goal, {status})
  }
  const pauseGoal = (goal: AssistantGoal) => mutate(() => lumenApi.controlGoal({
    action: 'pause', goal_id: goal.id, expected_revision: goal.revision, request_id: crypto.randomUUID(), criteria: '',
  }))
  const restore = (item: ManagedItem) => mutate(() => lumenApi.editAssistant({
    action: item.kind === 'goal' ? 'restore_goal' : 'restore_resource', id: item.value.id,
    expected_revision: item.value.revision,
  }))
  const closeResource = () => {setResource(null); if (objectId) onNavigate?.('library')}
  const planGoal = async (id: string, requestId: string) => {
    if (!onPlanGoal || planningRequest.current) return
    planningRequest.current = true; setPlanning(true); setPlanningFailure(null)
    try { await onPlanGoal(id, requestId) }
    catch (reason) { setPlanningFailure({id, requestId, message: reason instanceof Error ? reason.message : '请稍后重试'}) }
    finally { planningRequest.current = false; setPlanning(false) }
  }
  const latest = inspecting
    ? (state?.occurrences.find((item) => item.id === inspecting.id) ??
      inspecting)
    : null
  return (
    <section
      className={`assistant-page is-${page}`}
      aria-label={title}
      ref={sectionRef}
      tabIndex={-1}
      onScroll={e => {viewRef.current.scrollTop = e.currentTarget.scrollTop}}
    >
      <div className="assistant-page-heading">
        <div>
          <h1>{goalDetail ? goalDetail.goal.title : title}</h1>
          <p>
            {goalDetail ? goalDetail.goal.intent || '查看进展、成果与下一步。' : page === 'feed'
              ? '你交给 Lumen 的事，有了新的进展。'
              : page === 'ideas'
                ? '从已有目标和资料中找到下一步，查看方案后开始任务。'
                : page === 'library'
                  ? '生成的文件自动收录，随时打开、下载或继续使用。'
                  : page === 'goals'
                    ? '把想做的事，变成明确的下一步。'
                    : '明确范围、试跑，再让 Lumen 持续为你处理。'}
          </p>
        </div>
        {page === 'feed' && (
          <button
            disabled={collectionBusy}
            onClick={() => setCollectionEpoch((value) => value + 1)}
          >
            刷新
          </button>
        )}
        {page === 'automations' && (
          <button
            className="assistant-primary"
            disabled={ownerDisabled}
            onClick={() => setEditor('new')}
          >
            <Plus size={16} />
            新建持续跟进
          </button>
        )}
        {page === 'goals' && !goalDetail && (
          <button
            className="assistant-primary"
            disabled={ownerDisabled}
            onClick={() => setCreatingGoal(true)}
          >
            <Plus size={16} />
            新建目标
          </button>
        )}
        {page === 'goals' && goalDetail && <button onClick={() => {setGoalDetail(null); onNavigate?.('goals')}}>返回目标</button>}
        {page === 'library' && (
          <div className="library-header-actions">
          <label className="library-search"><MagnifyingGlass size={17} aria-hidden="true" />
            <span className="sr-only">搜索资源</span><input value={query} onChange={e => setQuery(e.target.value)} placeholder="搜索资料" />
          </label>
          <details className="library-create"><summary><Plus size={16} aria-hidden="true" />创建 <CaretDown size={13} aria-hidden="true" /></summary><div>
          <button
            className="assistant-primary"
            disabled={ownerDisabled}
            onClick={() => setCreatingNote(true)}
          >
            <Plus size={16} />
            新建页面
          </button>
          <button disabled={ownerDisabled} onClick={() => importRef.current?.click()}>导入文件</button>
          <input ref={importRef} type="file" hidden aria-label="导入文件" onChange={e => {
            const file = e.target.files?.[0]; if (file) void importFile(file); e.target.value = ''
          }} />
          <button disabled={ownerDisabled} onClick={() => setCreatingCollection(true)}>新建集合</button>
          </div></details>
          </div>
        )}
      </div>
      {planning && <p className="assistant-notice" role="status">目标已保存，正在打开关联对话并开始规划…</p>}
      {planningFailure && <div className="assistant-notice is-attention" role="alert">
        <div><strong>目标已保存，规划尚未开始</strong><p>{planningFailure.message}</p></div>
        <button disabled={planning} onClick={() => void planGoal(planningFailure.id, planningFailure.requestId)}>重试规划</button>
        <button onClick={() => onDiscuss({intent: '', context: {kind: 'goal', id: planningFailure.id}})}>打开关联对话</button>
      </div>}
      {(error || readError || collectionError) && (
        <div role="alert" className="assistant-notice">
          {error || readError || collectionError}
          <button
            onClick={() => {
              setCollectionEpoch((value) => value + 1)
              void load().catch(() => {})
            }}
          >
            重新读取
          </button>
        </div>
      )}
      {!state ? (
        <p role="status">正在读取个人助理…</p>
      ) : (
        <>
          {(!state.owner || state.status !== 'running') && (
            <div className="assistant-notice" role="status">
              {state.status === 'storage_error'
                ? '存储出现问题，已停止新执行。请检查 Host 日志和存储空间。'
                : '自动化由另一 Host 管理。请连接它以编辑，当前可查看已保存内容。'}
            </div>
          )}
          {page === 'feed' && (
            <>
              <div className="assistant-tabs" aria-label="信息流筛选">
                {[
                  ['all', '为你整理'],
                  ['attention', '需要你处理'],
                  ['unread', '未读'],
                ].map(([id, label]) => (
                  <button
                    key={id}
                    aria-pressed={filter === id}
                    onClick={() => setFilter(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="assistant-feed-layout">
                <div>
                  {feedItems.map((item) => {
                    const preview = state.resources.find(resource => resource.id === item.resource_id)
                    return (
                    <article className={`assistant-feed-card ${item.result !== 'complete' ? 'is-attention' : 'is-result'} ${preview ? 'has-preview' : ''}`} key={item.id}>
                      <div className="assistant-meta">
                        <span className="assistant-card-kind">{item.result === 'complete' ? <CheckCircle size={16} /> : <WarningCircle size={16} />}
                          {item.resource_id ? '成果更新' : item.goal_id ? '目标进展' : '持续跟进'}</span>
                        <span>{date(item.created_at)}{!item.read && ' · 未读'}</span>
                      </div>
                      <h3>{item.title}</h3>
                      <span className="assistant-status" data-status={item.result}>{resultLabels[item.result]}</span>
                      <p>{item.detail}</p>
                      <div className="assistant-card-actions">
                        {item.resource_id && (
                          <button
                            className="assistant-primary"
                            onClick={() =>
                              void openResource(item.resource_id!, !item.resource_revision, item.resource_revision ?? undefined)
                            }
                          >
                            查看本次成果 <ArrowUpRight size={16} />
                          </button>
                        )}
                        {!item.resource_id && item.goal_id && <button className="assistant-primary" onClick={() => visitGoal(item.goal_id!)}>查看目标进展 <ArrowUpRight size={16} /></button>}
                        <button
                          onClick={() =>
                            onDiscuss({intent: `请继续讨论：${item.title}`,
                              context: item.resource_id ? {kind: 'resource', id: item.resource_id, revision: item.resource_revision} :
                                item.goal_id ? {kind: 'goal', id: item.goal_id} : undefined})
                          }
                        >
                            {item.resource_id ? '围绕成果讨论' : '继续聊聊'}
                        </button>
                        <details className="assistant-card-more"><summary>更多 <CaretDown size={13} /></summary><div>
                        {item.occurrence_id && (
                          <button
                            onClick={() => void inspect(item.occurrence_id!)}
                          >
                            执行详情
                          </button>
                        )}
                        {item.goal_id && <button onClick={() => visitGoal(item.goal_id!)}>打开关联目标</button>}
                        {item.session_id && <button onClick={() => onOpenSession(item.session_id!)}>打开原对话</button>}
                        <button
                          disabled={ownerDisabled}
                          onClick={() =>
                            void mutate(() =>
                              lumenApi.editAssistant({
                                action: 'read',
                                id: item.id,
                                value: !item.read,
                              }),
                            )
                          }
                        >
                          {item.read ? '标为未读' : '标为已读'}
                        </button>
                        </div></details>
                        {item.resource_id && (
                          <button
                            aria-label={`收藏 ${item.title}`}
                            aria-pressed={item.saved}
                            disabled={ownerDisabled}
                            onClick={() =>
                              void mutate(() =>
                                lumenApi.editAssistant({
                                  action: 'save',
                                  id: item.resource_id!,
                                  value: !item.saved,
                                }),
                              )
                            }
                          >
                            <BookmarkSimple size={17} />
                          </button>
                        )}
                      </div>
                      {preview && <button className="assistant-feed-preview" aria-label={`预览 ${item.title}`} onClick={() => void openResource(item.resource_id!, false, item.resource_revision ?? undefined)}>
                        <ResourceThumbnail resource={{...preview, title: item.title, revision: item.resource_revision ?? preview.revision}} />
                      </button>}
                    </article>
                  )})}
                  {feedItems.length === 0 &&
                    !collectionBusy &&
                    !collectionError && (
                      <Empty
                        title={
                          filter === 'all'
                            ? '有进展时，会出现在这里'
                            : '这个筛选下没有记录'
                        }
                        detail="新的成果、目标进展和需要你决定的事，会汇集在这里。先从一个想完成的目标开始。"
                        action={filter === 'all' ? <button className="assistant-primary" onClick={() => onNavigate?.('goals')}>去创建目标 <ArrowUpRight size={16} /></button> : undefined}
                      />
                    )}
                </div>
                <aside className="assistant-feed-aside"><details open={feedExpanded} onToggle={event => setFeedExpanded(event.currentTarget.open)}><summary>正在为你跟进 <CaretDown size={14} /></summary>
                  <h3>正在为你跟进</h3>
                  {state.definitions
                    .filter((d) => d.enabled)
                    .map((d) => (
                      <div className="assistant-aside-row" key={d.id}>
                      <button className="assistant-aside-link" onClick={() => onNavigate?.('automations', d.id)}>{d.spec.name}<ArrowUpRight size={14} /></button>
                        <small>
                          {d.next_due ? `下次 ${date(d.next_due)}` : '等待安排'}{' '}
                          · {d.spec.schedule?.timezone}
                        </small>
                      </div>
                    ))}
                  {!state.definitions.some((d) => d.enabled) && (
                    <p>还没有启用的持续任务。</p>
                  )}
                  <h3>你的目标</h3>
                  {state.goals.filter((g) => !g.deleted_at).map((g) => (
                    <div className="assistant-aside-row" key={g.id}>
                      <button className="assistant-aside-link" onClick={() => visitGoal(g.id)}>{g.title}<ArrowUpRight size={14} /></button>
                      <small>
                        {g.milestones.filter((m) => m.done).length} /{' '}
                        {g.milestones.length} 个里程碑
                      </small>
                    </div>
                  ))}
                  {!state.goals.some((g) => !g.deleted_at) && <p>还没有目标。</p>}
                  <p>
                    本机服务运行时可持续跟进；退出、睡眠或关机时暂停。
                  </p>
                </details></aside>
              </div>
            </>
          )}
          {page === 'ideas' && (
            <>
              <div className="assistant-tabs" aria-label="灵感分类">
                {[
                  ['all', '全部建议'],
                  ['work', '工作'],
                  ['life', '生活'],
                  ['learning', '学习'],
                  ['hidden', '已忽略'],
                ].map(([id, label]) => (
                  <button
                    key={id}
                    aria-pressed={filter === id}
                    onClick={() => setFilter(id)}
                  >
                    {label}
                  </button>
                ))}
              </div>
              <div className="assistant-idea-list">
                {!state.ideas.some(idea => filter === 'hidden' ? idea.dismissed : !idea.dismissed && (filter === 'all' || filter === idea.category)) &&
                  <Empty title={filter === 'hidden' ? '没有已忽略的建议' : '暂时没有相关建议'} detail="目标和资料有变化时，相关建议会在这里更新。" />}
                {state.ideas
                  .filter((idea) =>
                    filter === 'hidden'
                      ? idea.dismissed
                      : !idea.dismissed &&
                        (filter === 'all' || filter === idea.category),
                  )
                  .map((idea) => (
                    <IdeaWorkbench key={idea.id} idea={idea} disabled={ownerDisabled}
                      onStart={onStartIdea}
                      onOpen={() => onNavigate?.(idea.destination === 'goal' ? 'goals' : 'automations', idea.target_id ?? undefined)}
                      onDismiss={() =>
                            void mutate(() =>
                              lumenApi.editAssistant({
                                action: 'idea',
                                id: idea.id,
                                value: !idea.dismissed,
                              }),
                            )
                          } />
                  ))}
              </div>
              <p className="assistant-footnote">
                建议来自未完成的目标与现有资料；通用起点会明确标注。查看方案不会执行，开始任务才会运行。
              </p>
            </>
          )}
          {page === 'library' && (
            <>
              <div className="library-toolbar">
                <nav className="library-types" aria-label="资料类型">
                  {libraryTypes.map(([value, label]) => <button type="button" key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}
                </nav>
                <div className="library-view-actions">
                <button type="button" aria-label="已收藏" aria-pressed={filter === 'saved'} onClick={() => setFilter(filter === 'saved' ? 'all' : 'saved')}><BookmarkSimple size={18} /></button>
                <select
                  aria-label="资源类型"
                  value={libraryTypes.some(([value]) => value === filter) || filter === 'saved' ? '' : filter}
                  onChange={(e) => setFilter(e.target.value)}
                >
                  <option value="" disabled>更多</option>
                  <option value="code">代码</option>
                  <option value="file">其他文件</option>
                  <option value="report">简报</option>
                  <option value="work_product">对话产物</option>
                  <option value="trial">试跑预览</option>
                  <option value="materials">执行材料与未收录</option>
                  <option value="trash">回收站</option>
                  {(state.collections ?? []).map(c => <option key={c.id} value={`collection:${c.id}`}>{c.title}</option>)}
                </select>
                <button aria-label="3D 书架视图" title="3D 书架" disabled={filter === 'trash'} aria-pressed={showShelf} onClick={() => setShelf(true)}><Books size={19} /></button>
                <button aria-label="网格视图" aria-pressed={!showShelf && grid} onClick={() => { setShelf(false); setGrid(true) }}><SquaresFour size={18} /></button>
                <button aria-label="列表视图" aria-pressed={!showShelf && !grid} onClick={() => { setShelf(false); setGrid(false) }}><ListBullets size={19} /></button>
                <button aria-label="刷新资料" disabled={collectionBusy} onClick={() => setCollectionEpoch(value => value + 1)}><ArrowClockwise size={17} /></button>
                </div>
              </div>
              {filter === 'trash' && <p className="assistant-footnote">删除的资料保留在这里，恢复后可继续使用。原文件与对话不会被删除。</p>}
              {filter.startsWith('collection:') && state.collections.filter(c => filter === `collection:${c.id}`).map(c => <div className="library-collection-heading" key={c.id}>
                <h2>{c.title}</h2><button disabled={ownerDisabled} aria-label={`管理集合 ${c.title}`} onClick={e => setMenu({anchor: e.currentTarget, item: {kind: 'collection', value: c}})}><DotsThree size={21} /></button>
              </div>)}
              {showShelf && libraryItems.length > 0 && !resource && <LibraryScene key={`${filter}:${query}`} resources={libraryItems} onOpen={id => void openResource(id)} />}
              {!showShelf && !grid && <div className="library-columns" aria-hidden="true"><span>名称</span><span>来源</span><span>最近更新</span><span /></div>}
              {!showShelf && <div className={`assistant-resource-list ${grid ? 'is-grid' : ''}`}>
                {libraryItems.map((r) => (
                  <article key={r.id}>
                    <button className="library-resource-open" aria-label={`打开 ${r.title}`} disabled={!!r.deleted_at} onClick={() => void openResource(r.id)}>
                      {grid && <span className="library-cover"><ResourceThumbnail resource={r} /></span>}
                      <span className="library-file-icon"><ResourceIcon type={r.file_type} /></span>
                      <span className="library-resource-name"><strong>{r.title}</strong>
                        <small>{resourceTypeLabel(r)}{r.byte_size != null ? ` · ${r.byte_size < 1024 ? `${r.byte_size} B` : `${Math.ceil(r.byte_size / 1024)} KB`}` : ''}{r.status !== 'active' ? ' · 需要核实来源' : ''}</small>
                      </span>
                    </button>
                    {r.session_id ? <button className="library-resource-source" onClick={() => onOpenSession(r.session_id!)} title="打开原对话"><ChatCircle size={15} />对话</button>
                      : <span className="library-resource-source">{r.kind === 'report' ? '持续跟进' : '资料空间'}</span>}
                    <time className="library-resource-date" dateTime={r.deleted_at || r.updated_at || r.created_at}>{date(r.deleted_at || r.updated_at || r.created_at)}</time>
                    <div className="library-row-actions">
                    {r.deleted_at ? <button aria-label={`恢复 ${r.title}`} disabled={ownerDisabled} onClick={() => void restore({kind: 'resource', value: r})}><ArrowCounterClockwise size={18} /></button> : <>
                    <button
                      aria-label={`收藏 ${r.title}`}
                      className="library-bookmark"
                      aria-pressed={r.saved}
                      disabled={ownerDisabled}
                      onClick={() =>
                        void mutate(() =>
                          lumenApi.editAssistant({
                            action: 'save',
                            id: r.id,
                            value: !r.saved,
                          }),
                        )
                      }
                    >
                      <BookmarkSimple
                        size={19}
                        weight={r.saved ? 'fill' : 'regular'}
                      />
                    </button>
                    <button disabled={ownerDisabled} aria-label={`管理资料 ${r.title}`} onClick={e => setMenu({anchor: e.currentTarget, item: {kind: 'resource', value: r}})}><DotsThree size={21} /></button>
                    </>}
                    </div>
                  </article>
                ))}
              </div>}
              {libraryItems.length === 0 &&
                !collectionBusy &&
                !collectionError && (
                  <Empty
                    title={
                      query || filter !== 'all'
                        ? '没有找到匹配的资源'
                        : '你的成果，会在这里汇集'
                    }
                        detail="生成的文档、网页、图片与其他文件会自动收录。也可以创建页面或导入已有资料。"
                        action={query || filter !== 'all' ? undefined : <><button className="assistant-primary" onClick={() => setCreatingNote(true)}><Plus size={16} />新建页面</button><button onClick={() => importRef.current?.click()}>导入资料</button></>}
                  />
                )}
            </>
          )}
          {page === 'goals' && (
            <div className="assistant-goals">
              {goalLoading && !goalDetail && <p role="status">正在读取目标…</p>}
              {!goalDetail && !goalLoading && <div className="goal-list-toolbar">
                <nav aria-label="目标状态">{[['all', '全部'], ['active', '未完成'], ['completed', '已完成'], ['archived', '已归档'], ['trash', '回收站']].map(([value, label]) =>
                  <button key={value} aria-pressed={filter === value} onClick={() => setFilter(value)}>{label}</button>)}</nav>
                <label><MagnifyingGlass size={17} /><span className="sr-only">搜索目标</span><input placeholder="搜索目标" value={query} onChange={e => setQuery(e.target.value)} /></label>
              </div>}
              {filter === 'trash' && !goalDetail && <p className="assistant-footnote">已删除目标可在这里恢复，原对话与成果仍保留。</p>}
              {goalDetail && <>
                <div className="assistant-goal-summary">
                  <span className="assistant-status" data-status={goalDetail.goal.execution?.status ?? goalDetail.goal.status}>{goalStatusLabels[goalDetail.goal.status]}{goalDetail.goal.execution ? ` · ${goalExecutionLabels[goalDetail.goal.execution.status]}` : ''}</span>
                  <div className="assistant-card-actions">
                    {goalDetail.goal.execution?.status === 'review' && goalDetail.goal.status === 'active' && <button className="assistant-primary" disabled={ownerDisabled} onClick={() => requestGoalStatus(goalDetail.goal, 'completed')}>确认完成目标 <CheckCircle size={16} /></button>}
                    <button className={goalDetail.goal.execution?.status === 'review' ? '' : 'assistant-primary'} disabled={goalDetail.goal.status === 'archived'} onClick={() => onDiscuss({
                      intent: '', context: {kind: 'goal', id: goalDetail.goal.id},
                    })}>{goalDetail.goal.milestones.length ? '继续讨论' : '开始规划'}</button>
                    <button disabled={ownerDisabled || goalDetail.goal.execution?.status === 'active'} onClick={() => setEditingGoal(goalDetail.goal)}>编辑目标</button>
                    <button disabled={ownerDisabled} onClick={e => setMenu({anchor: e.currentTarget, item: {kind: 'goal', value: goalDetail.goal}})}>管理目标 <DotsThree size={18} /></button>
                  </div>
                  <section className="goal-detail-section" aria-label="目标成果">
                    <h3>成果</h3>
                    <div className="goal-results-grid">{goalDetail.resources.map(r => <button className="goal-resource-link" key={r.id} onClick={() => void openResource(r.id, false, r.revision)}><ResourceIcon type={r.file_type} /><span>{r.title}<small>{resourceTypeLabel(r)} · 版本 {r.revision}</small></span><ArrowUpRight size={16} /></button>)}</div>
                    {!goalDetail.resources.length && <p>尚无成果文件。围绕目标生成的文件会自动关联到这里。</p>}
                  </section>
                  <section className="goal-detail-section goal-execution" aria-label="目标推进">
                    <div className="goal-section-heading"><h3>目标推进</h3>
                      <span>{goalDetail.goal.execution ? goalExecutionLabels[goalDetail.goal.execution.status] : '尚未启动'}</span></div>
                    <p>{goalDetail.goal.execution?.detail || '保存目标不会执行。明确成功标准后，可以让 Lumen 持续推进并核对结果。'}</p>
                    {goalDetail.goal.execution && <>
                      <p className="goal-execution-criteria">成功标准：{goalDetail.goal.execution.criteria}</p>
                      <p className="assistant-meta">已执行 {goalDetail.goal.execution.turns} 轮{goalDetail.goal.execution.turn_limit ? ` / 上限 ${goalDetail.goal.execution.turn_limit} 轮` : ' · 未设轮数上限'}。每轮仍遵守原有工具、审批和运行预算。</p>
                    </>}
                    <div className="assistant-card-actions">
                      {goalDetail.goal.execution?.status === 'active'
                        ? <button disabled={ownerDisabled} onClick={() => void pauseGoal(goalDetail.goal)}>暂停推进</button>
                        : <button disabled={ownerDisabled || goalDetail.goal.status !== 'active'} onClick={() => setStartingGoal(goalDetail.goal)}>{!goalDetail.goal.execution || ['review', 'accepted'].includes(goalDetail.goal.execution.status) ? '启动目标推进' : '恢复目标推进'}</button>}
                      {goalDetail.goal.primary_session_id && <button onClick={() => onOpenSession(goalDetail.goal.primary_session_id!)}>查看关联对话</button>}
                    </div>
                    {goalDetail.goal.execution?.evidence?.length ? <details className="goal-evidence"><summary>查看推进核验记录</summary>
                      {goalDetail.goal.execution.evidence.map(e => <div key={e.run_id}><p>{e.summary}</p><small>{e.call_ids.length} 份工具回执 · 执行 {e.run_id.slice(0, 8)}</small></div>)}
                    </details> : null}
                  </section>
                  <section className="goal-detail-section" aria-label="目标里程碑">
                    <div className="goal-section-heading"><h3>里程碑</h3>
                      <button disabled={ownerDisabled || goalDetail.goal.execution?.status === 'active'} onClick={() => setEditingGoal(goalDetail.goal)}>编辑里程碑</button></div>
                    <p>{goalDetail.goal.milestones.length
                      ? `${goalDetail.goal.milestones.filter(m => m.done).length} / ${goalDetail.goal.milestones.length} 项已完成 · 完成状态由你确认`
                      : '先和 Lumen 讨论现状与成功标准，再确认下一步。也可以手动添加里程碑。'}</p>
                    {goalDetail.goal.milestones.map(m => <label className="assistant-milestone" key={m.id}>
                      <input type="checkbox" checked={m.done} disabled={ownerDisabled || goalDetail.goal.status === 'archived'}
                        onChange={e => void updateGoal(goalDetail.goal, {milestones: goalDetail.goal.milestones.map(item =>
                          item.id === m.id ? {...item, done: e.target.checked} : item)})} />
                      <span>{m.title}</span>
                    </label>)}
                  </section>
                  <section className="goal-detail-section" aria-label="目标参考资料">
                  <h3>参考资料</h3>
                  {goalDetail.context_resources.map(r => <p key={r.id}><button onClick={() => void openResource(r.id)}>{r.title}</button>
                    <button disabled={ownerDisabled} onClick={() => void mutate(() => lumenApi.editAssistant({
                      action: 'link_resource', id: r.id, goal_id: goalDetail.goal.id, value: false,
                    }))}>取消关联</button></p>)}
                  <button onClick={() => onNavigate?.('library')}>去资料空间添加资料</button>
                  </section>
                  <details className="goal-follow-ups">
                  <summary>持续跟进（可选）{goalDetail.definitions.length > 0 && ` · ${goalDetail.definitions.filter(d => d.enabled).length} 项已启用`}</summary>
                  <p>需要定期处理时，再单独设置来源与时间。保存目标、勾选里程碑都不会启用跟进。</p>
                  <button disabled={ownerDisabled || goalDetail.goal.status !== 'active'} onClick={() => {setFollowGoal(goalDetail.goal); setEditor('new')}}>设置持续跟进</button>
                  {goalDetail.definitions.map(d => <div className="assistant-aside-row" key={d.id}>
                    <strong>{d.spec.name}</strong><p>{d.enabled ? `已启用 · 下次 ${d.next_due ? date(d.next_due) : '待安排'}` : '未启用'}{d.spec.schedule ? ` · ${d.spec.schedule.timezone}` : ' · 仅手动运行'}</p>
                    <div className="assistant-card-actions">
                      <button disabled={ownerDisabled} onClick={() => {setFollowGoal(goalDetail.goal); setEditor(d)}}>调整</button>
                      <button disabled={ownerDisabled} onClick={() => action(d, 'trial')}>试跑</button>
                      <button disabled={ownerDisabled || (!d.enabled && !d.validation)} onClick={() => action(d, d.enabled ? 'pause' : 'enable')}>{d.enabled ? '暂停' : '启用'}</button>
                      <button disabled={ownerDisabled || !d.validation} onClick={() => action(d, 'run')}>立即运行</button>
                    </div>
                    {state.occurrences.filter(o => o.automation_id === d.id).slice(0, 1).map(o => <button key={o.id} onClick={() => void inspect(o.id)}>最近执行 · {resultLabels[o.result ?? ''] || o.phase}</button>)}
                  </div>)}
                  </details>
                </div>
              </>}
              <div className="assistant-goal-grid">{!goalDetail && !goalLoading && state.goals.filter(g =>
                (filter === 'trash' ? !!g.deleted_at : !g.deleted_at && (filter === 'all' || g.status === filter))
                && `${g.title} ${g.intent}`.toLowerCase().includes(query.trim().toLowerCase())).map((goal) => (
                <article key={goal.id} data-status={goal.execution?.status ?? goal.status}>
                  {!goalDetail && <div className="goal-row-heading"><h3><button disabled={!!goal.deleted_at} onClick={() => visitGoal(goal.id)}>{goal.title}</button></h3>
                    <span className="assistant-status" data-status={goal.execution?.status ?? goal.status}>{goalStatusLabels[goal.status]}{goal.execution ? ` · ${goalExecutionLabels[goal.execution.status]}` : ' · 尚未启动'}</span>
                    {goal.deleted_at ? <button disabled={ownerDisabled} onClick={() => void restore({kind: 'goal', value: goal})}>恢复目标</button>
                      : <button disabled={ownerDisabled} aria-label={`管理目标 ${goal.title}`} onClick={e => setMenu({anchor: e.currentTarget, item: {kind: 'goal', value: goal}})}><DotsThree size={21} /></button>}
                  </div>}
                  {!goalDetail && goal.intent && <p className="goal-intent">{goal.intent}</p>}
                  {!goalDetail && goal.execution?.detail && <p className="goal-last-progress">{goal.execution.detail}</p>}
                  {!goalDetail && !goal.deleted_at && goal.status !== 'archived' && <div className="assistant-card-actions">
                    <button className="assistant-primary" onClick={() => goal.execution ? visitGoal(goal.id) : onDiscuss({intent: '', context: {kind: 'goal', id: goal.id}})}>{goal.execution?.status === 'review' ? '验收成果' : goal.execution ? '查看进展' : goal.milestones.length ? '继续讨论' : '开始规划'}<ArrowUpRight size={16} /></button>
                  </div>}
                  <p>
                    {goal.milestones.length ? <>{goal.milestones.filter((m) => m.done).length} /{' '}
                    {goal.milestones.length} 个里程碑已确认
                    </> : '尚未确认计划'}
                  </p>
                  {!!goal.milestones.length && <progress className="goal-milestone-progress" aria-label="已确认里程碑" value={goal.milestones.filter(m => m.done).length} max={goal.milestones.length} />}
                </article>
              ))}</div>
              {!goalDetail && !goalLoading && !state.goals.some(g => (filter === 'trash' ? !!g.deleted_at : !g.deleted_at && (filter === 'all' || g.status === filter))
                && `${g.title} ${g.intent}`.toLowerCase().includes(query.trim().toLowerCase())) && (
                <Empty
                  title={filter === 'trash' ? '回收站为空' : query ? '没有匹配的目标' : '从一个明确的目标开始'}
                  detail={filter === 'trash' ? '删除的目标会出现在这里。' : '只需写下想达成什么。与 Lumen 一起规划，再决定是否持续跟进。'}
                  action={filter === 'trash' || query ? undefined : <button className="assistant-primary" onClick={() => setCreatingGoal(true)}><Plus size={16} />创建第一个目标</button>}
                />
              )}
            </div>
          )}
          {page === 'automations' && (
            <>
              {state.active_run_id && state.active_session_id && <div className="assistant-notice">
                正在执行任务
                <button onClick={() => onOpenSession(state.active_session_id!)}>打开当前会话</button>
                <button disabled={busy} onClick={() => void mutate(() => lumenApi.cancelRun(state.active_run_id!))}>停止本次执行</button>
              </div>}
              {!state.workspace_source_available && (
                <div className="assistant-notice">
                  当前平台不支持安全的工作区来源读取，不能试跑或启用。文件自动化目前支持
                  macOS 和 Linux。
                </div>
              )}
              <div className="assistant-notice">
                在这里检查全部跟进，暂停安排或停止本次运行。Host 退出、休眠时不执行。
                {state.research_available ? '已具备受限的后台联网检索能力。' : '后台联网尚不可用：需配置内建 web_search 并在权限设置中明确允许。前台对话仍可使用原有能力。'}
              </div>
              {state.definitions.map((d) => {
                const occurrence = state.occurrences.find(
                  (o) => o.automation_id === d.id,
                )
                return (
                  <article className="assistant-automation" key={d.id}>
                    <div>
                      <h3>{d.spec.name}</h3>
                      <p>{d.spec.intent}</p>
                      <small>
                        {d.spec.schedule?.kind === 'weekly'
                          ? '每周'
                          : d.spec.schedule?.kind === 'weekdays'
                            ? '工作日'
                            : '每天'}{' '}
                        {String(d.spec.schedule?.hour).padStart(2, '0')}:
                        {String(d.spec.schedule?.minute).padStart(2, '0')} ·{' '}
                        {d.spec.schedule?.timezone} ·{' '}
                        {d.enabled ? '已启用' : '未启用'}
                      </small>
                      <p className="assistant-meta">
                        {d.spec.paths.join('、')} · 最多 {d.spec.request_limit}{' '}
                        个模型步骤（重试另计） · {d.spec.timeout_seconds} 秒
                      </p>
                      {occurrence && (
                        <button
                          className="assistant-latest"
                          onClick={() => void inspect(occurrence.id)}
                        >
                          {occurrence.phase === 'settled'
                            ? resultLabels[occurrence.result ?? 'failed']
                            : occurrence.phase === 'queued'
                              ? '等待工作区空闲'
                              : occurrence.phase === 'settling'
                                ? '正在保存结果'
                                : '执行中'}{' '}
                          · {date(occurrence.created_at)} →
                        </button>
                      )}
                    </div>
                    <div className="assistant-card-actions">
                      <button
                        disabled={ownerDisabled}
                        onClick={() => setEditor(d)}
                      >
                        编辑
                      </button>
                      <button
                        disabled={
                          ownerDisabled || (d.spec.profile === 'workspace_snapshot' && !state.workspace_source_available)
                        }
                        onClick={() => action(d, 'trial')}
                      >
                        试跑
                      </button>
                      <button
                        disabled={
                          ownerDisabled ||
                          (!d.enabled && !d.validation)
                        }
                        onClick={() =>
                          action(d, d.enabled ? 'pause' : 'enable')
                        }
                      >
                        {d.enabled ? '暂停' : '启用'}
                      </button>
                      <button
                        disabled={
                          ownerDisabled ||
                          !d.validation ||
                          (d.spec.profile === 'workspace_snapshot' && !state.workspace_source_available)
                        }
                        onClick={() => action(d, 'run')}
                      >
                        立即运行
                      </button>
                    </div>
                  </article>
                )
              })}
              {state.definitions.length === 0 && (
                <Empty
                  title="把反复要做的事，交给 Lumen"
                  detail="选择来源、设置时间，先成功试跑，再启用。"
                />
              )}
            </>
          )}
        </>
      )}
      {(page === 'feed' || page === 'library') && (
        <div className="assistant-pagination">
          {collectionBusy && <p role="status">正在读取记录…</p>}
          {cursor !== null && (
            <button disabled={collectionBusy} onClick={() => void more()}>
              读取更早的记录
            </button>
          )}
        </div>
      )}
      {editor && (
        <AutomationEditor
          error={error}
          definition={editor === 'new' ? null : editor}
          intent={followGoal?.intent || followGoal?.title || initialIntent}
          researchAvailable={state?.research_available ?? false}
          linkedGoal={followGoal?.id}
          busy={busy}
          onClose={() => {setEditor(null); setFollowGoal(null)}}
          onSave={async (spec, requestId) => {
            const saved = await mutate(() =>
              lumenApi.automation({
                action: editor === 'new' ? 'create' : 'update',
                request_id: requestId,
                id: editor === 'new' ? undefined : editor.id,
                expected_revision:
                  editor === 'new' ? undefined : editor.revision,
                spec,
                goal_id: editor === 'new' ? followGoal?.id : undefined,
              }),
            )
            if (saved) setEditor(null)
          }}
        />
      )}
      {(creatingGoal || editingGoal) && (
        <GoalEditor
          error={error}
          busy={busy}
          initialGoal={editingGoal ?? undefined}
          initialTitle={resource?.resource.title || initialIntent}
          onClose={() => {setCreatingGoal(false); setEditingGoal(null)}}
          onSave={async (goal, requestId) => {
            const identity = editingGoal?.id ?? requestId
            const saved = await mutate(async () => {
              await lumenApi.editAssistant({ action: 'goal', id: identity, goal,
                expected_revision: editingGoal?.revision, source_session_id: editingGoal ? undefined : sourceSessionId })
              if (resource && !editingGoal) await lumenApi.editAssistant({action: 'link_resource', id: resource.resource.id, goal_id: identity})
            })
            if (saved) {
              setCreatingGoal(false); setEditingGoal(null); setResource(null)
              if (!editingGoal) onGoalCreated?.()
              visitGoal(identity)
              if (!editingGoal) await planGoal(identity, requestId)
            }
          }}
        />
      )}
      {startingGoal && <GoalExecutionEditor goal={startingGoal} busy={busy} error={error}
        onClose={() => setStartingGoal(null)} onSave={async (criteria, turnLimit, requestId) => {
          const saved = await mutate(() => lumenApi.controlGoal({
            action: startingGoal.execution && !['review', 'accepted'].includes(startingGoal.execution.status) ? 'resume' : 'start',
            goal_id: startingGoal.id, expected_revision: startingGoal.revision, request_id: requestId,
            criteria, turn_limit: turnLimit,
          }))
          if (saved) setStartingGoal(null)
        }} />}
      {closingGoal && <Panel title={closingGoal.status === 'completed' ? '完成目标' : '归档目标'} busy={busy} onClose={() => setClosingGoal(null)}>
        <p>「{closingGoal.goal.title}」仍有已启用的持续跟进。请选择如何收尾。</p>
        <ul>{state?.definitions.filter(d => closingGoal.goal.automation_ids.includes(d.id) && d.enabled).map(d => <li key={d.id}>{d.spec.name}</li>)}</ul>
        <p>暂停会取消尚未执行的排队任务，已经开始的执行需要先停止或等待结束。保留后，跟进仍会按原安排运行。</p>
        {error && <p role="alert">{error}</p>}
        <div className="assistant-card-actions">{(['pause', 'keep'] as const).map(policy => <button key={policy} disabled={busy} className={policy === 'pause' ? 'assistant-primary' : ''} onClick={() => void (async () => {
          const g = closingGoal.goal
          const saved = await mutate(() => lumenApi.editAssistant({action: 'goal', id: g.id, expected_revision: g.revision,
            goal: {title: g.title, intent: g.intent, status: closingGoal.status, milestones: g.milestones, automation_ids: g.automation_ids}, follow_up_policy: policy}))
          if (saved) setClosingGoal(null)
        })()}>{closingGoal.status === 'completed' ? '完成' : '归档'}并{policy === 'pause' ? '暂停跟进' : '保留跟进'}</button>)}</div>
      </Panel>}
      {(creatingNote || editingResource) && (
        <NoteEditor
          error={error}
          initialTitle={editingResource?.resource.title}
          initialContent={editingResource?.content}
          busy={busy}
          onClose={() => {
            setCreatingNote(false)
            setEditingResource(null)
          }}
          onSave={async (title, content, requestId) => {
            const saved = await mutate(() =>
              lumenApi.editAssistant({
                action: editingResource ? 'edit_resource' : 'note',
                id: editingResource?.resource.id ?? requestId,
                expected_revision: editingResource?.resource.revision,
                title,
                content,
              }),
            )
            if (saved) {
              setCreatingNote(false)
              setEditingResource(null)
            }
          }}
        />
      )}
      {resource?.resource.file_path && !fileDetails && <DocumentPreview key={`${resource.resource.id}:${resource.resource.revision}`}
        path={resource.resource.file_path} resource={resource.resource}
        onClose={closeResource}
        actions={<><button onClick={() => {onDiscuss({intent: '', context: {kind: 'resource', id: resource.resource.id, revision: resource.resource.revision}}); setResource(null)}}>继续讨论</button>
          <button onClick={() => setFileDetails(true)}>资料详情</button></>} />}
      {resource && (!resource.resource.file_path || fileDetails) && (
        <Panel
          title={resource.resource.title}
          onClose={closeResource}
        >
          {resource.historical && (
            <p className="assistant-footnote">
              本次执行的原始结果，后续编辑不会改写此版本。
            </p>
          )}
          {resource.resource.file_path ? <><p>{resource.resource.file_path}</p><button onClick={() => setFileDetails(false)}>打开文件预览</button></> : <MarkdownMessage content={resource.content} />}
          <p className="assistant-footnote">版本 {resource.resource.revision} · {date(resource.resource.updated_at || resource.resource.created_at)}</p>
          {resource.resource.revision > 1 && <button onClick={() => void openResource(resource.resource.id, false, resource.resource.revision - 1)}>查看上一版本</button>}
          {resource.historical && <button onClick={() => void openResource(resource.resource.id)}>打开当前版本</button>}
          {resource.resource.kind === 'work_product' &&
            resource.resource.session_id && (
              <button
                onClick={() => {
                  onOpenSession(resource.resource.session_id!)
                  setResource(null)
                }}
              >
                打开原对话与产物
              </button>
            )}
          {!resource.historical &&
            !resource.resource.file_path && resource.resource.kind !== 'work_product' && (
              <button
                onClick={() => {
                  setEditingResource(resource)
                  setResource(null)
                }}
              >
                编辑资源
              </button>
            )}
          <button
            onClick={() => {
              onDiscuss({intent: '', context: {kind: 'resource', id: resource.resource.id, revision: resource.resource.revision}})
              setResource(null)
            }}
          >
            继续聊聊
          </button>
          {!resource.historical && <div className="assistant-resource-organization">
            <button disabled={ownerDisabled} onClick={() => {setRenaming({kind: 'resource', value: resource.resource}); setRenameTitle(resource.resource.title)}}>重命名资料</button>
            <button disabled={ownerDisabled} onClick={() => setRemoving({kind: 'resource', value: resource.resource})}>删除资料</button>
            <button disabled={ownerDisabled} onClick={() => setCreatingGoal(true)}>从资料创建目标</button>
            <button disabled={ownerDisabled} onClick={() => void mutate(async () => {
              await lumenApi.editAssistant({action: 'visibility', id: resource.resource.id, value: !resource.resource.published})
              setResource(null)
            })}>{resource.resource.published ? '从默认列表移除' : '收录到资料空间'}</button>
            <h3>关联目标</h3>
            {(state?.goals ?? []).filter(g => !g.deleted_at).map(g => <label className="assistant-milestone" key={g.id}>
              <input type="checkbox" disabled={ownerDisabled} checked={resource.goal_ids?.includes(g.id) ?? false}
                onChange={e => {const value = e.target.checked; void mutate(async () => {
                  await lumenApi.editAssistant({action: 'link_resource', id: resource.resource.id, goal_id: g.id, value})
                  await openResource(resource.resource.id)
                })}} />{g.title}
            </label>)}
            <h3>资料集合</h3>
            {(state?.collections ?? []).map(c => <label className="assistant-milestone" key={c.id}>
              <input type="checkbox" disabled={ownerDisabled} checked={resource.collection_ids?.includes(c.id) ?? false}
                onChange={e => {const value = e.target.checked; void mutate(async () => {
                  await lumenApi.editAssistant({action: 'collect', id: resource.resource.id, collection_id: c.id, value})
                  await openResource(resource.resource.id)
                })}} />{c.title}
            </label>)}
          </div>}
        </Panel>
      )}
      {latest && (
        <Panel title="执行详情" onClose={() => setInspecting(null)}>
          {error && (
            <p role="alert" className="settings-inline-error">
              {error}
            </p>
          )}
          <h3>
            {resultLabels[latest.result ?? ''] ??
              (latest.phase === 'queued'
                ? '等待工作区空闲'
                : latest.phase === 'settling'
                  ? '正在保存结果'
                  : '执行中')}
          </h3>
          <p>{latest.detail}</p>
          <p>
            {date(latest.created_at)} ·{' '}
            {latest.kind === 'trial' ? '试跑，不消费正式来源进度' : '正式执行'}
          </p>
          <h3>来源覆盖</h3>
          {latest.receipts.map((r) => (
            <p key={r.path}>
              <strong>{r.path}</strong> · {r.error || (r.coverage === 'search_snippet' ? '检索摘要' : r.coverage === 'page_excerpt' ? '网页节选' : '完整读取快照')}
              {r.read_at && ` · ${date(r.read_at)}`}
            </p>
          ))}
          {latest.artifact_ref && latest.result !== 'no_change' && (
            <button onClick={() => void openResource(latest.id, true)}>
              查看结果
            </button>
          )}
          <button
            disabled={!latest.session_available}
            onClick={() => {
              setInspecting(null)
              onOpenSession(latest.session_id)
            }}
          >
            打开执行会话
          </button>
          {latest.phase === 'running' && (
            <button
              disabled={busy}
              onClick={() =>
                void mutate(() => lumenApi.cancelRun(latest.interaction_id))
              }
            >
              停止本次执行
            </button>
          )}
          <p className="assistant-footnote">
            中断的执行不会自动重放。未知外部动作需先核实。
          </p>
        </Panel>
      )}
      {menu && <ActionsMenu anchor={menu.anchor} className="assistant-management-menu" label={`管理${menu.item.kind === 'goal' ? '目标' : menu.item.kind === 'collection' ? '集合' : '资料'}：${menu.item.value.title}`} onClose={() => setMenu(null)}>
        {menu.item.kind === 'goal' ? <>
          <button role="menuitem" disabled={menu.item.value.execution?.status === 'active'} onClick={() => {if (menu.item.kind === 'goal') setEditingGoal(menu.item.value); setMenu(null)}}><PencilSimpleLine size={17} />编辑目标</button>
          <button role="menuitem" disabled={menu.item.value.execution?.status === 'active'} onClick={() => {if (menu.item.kind === 'goal') requestGoalStatus(menu.item.value, menu.item.value.status === 'completed' ? 'active' : 'completed'); setMenu(null)}}>{menu.item.value.status === 'completed' ? '恢复为未完成' : '标记为已完成'}</button>
          <button role="menuitem" disabled={menu.item.value.execution?.status === 'active'} onClick={() => {if (menu.item.kind === 'goal') requestGoalStatus(menu.item.value, menu.item.value.status === 'archived' ? 'active' : 'archived'); setMenu(null)}}>{menu.item.value.status === 'archived' ? '取消归档' : '归档目标'}</button>
        </> : <>
          <button role="menuitem" onClick={() => {setRenaming(menu.item); setRenameTitle(menu.item.value.title); setMenu(null)}}><PencilSimpleLine size={17} />重命名</button>
          {menu.item.kind === 'resource' && <button role="menuitem" onClick={() => {void openResource(menu.item.value.id).then(() => setFileDetails(true)); setMenu(null)}}>详情与关联</button>}
          {menu.item.kind === 'resource' && !menu.item.value.file_path && menu.item.value.kind !== 'work_product' && <button role="menuitem" onClick={() => {
            const identity = menu.item.value.id; setMenu(null)
            void lumenApi.assistantResource(identity).then(value => setEditingResource(value)).catch(reason => setError(reason instanceof Error ? reason.message : '读取失败，请重试'))
          }}>编辑页面内容</button>}
        </>}
        <div className="session-actions-divider" role="separator" />
        <button role="menuitem" className="is-danger" onClick={() => {setRemoving(menu.item); setMenu(null)}}><Trash size={17} />{menu.item.kind === 'goal' ? '删除目标' : menu.item.kind === 'collection' ? '删除集合' : '删除资料'}</button>
      </ActionsMenu>}
      {renaming && <Panel title={renaming.kind === 'collection' ? '重命名集合' : '重命名资料'} onClose={() => setRenaming(null)} dirty={renameTitle !== renaming.value.title}>
        <form className="assistant-form" onSubmit={e => {e.preventDefault(); void mutate(() => lumenApi.editAssistant({
          action: renaming.kind === 'collection' ? 'collection' : 'rename_resource', id: renaming.value.id,
          title: renameTitle, expected_revision: renaming.value.revision,
        })).then(saved => {if (saved) {setRenaming(null); setResource(null)}})}}>
          {error && <p role="alert">{error}</p>}
          <label>名称<input required autoFocus maxLength={200} value={renameTitle} onChange={e => setRenameTitle(e.target.value)} /></label>
          {renaming.kind === 'resource' && <p className="assistant-footnote">仅修改资料显示名称，原文件名与历史内容保留。</p>}
          <button type="submit" disabled={busy || !renameTitle.trim()}>保存名称</button>
        </form>
      </Panel>}
      {removing && <Panel title={removing.kind === 'goal' ? '删除目标' : removing.kind === 'collection' ? '删除集合' : '删除资料'} onClose={() => {if (!busy) setRemoving(null)}}>
        <p>确认删除「{removing.value.title}」？</p>
        <p className="assistant-footnote">{removing.kind === 'collection' ? '仅删除集合与分组关系，里面的资料仍保留。' : removing.kind === 'goal'
          ? '目标移入回收站，原对话与成果仍保留。请先暂停关联的持续跟进并等待执行结束。'
          : '资料移入回收站，可随时恢复。不会删除工作区原文件或对话。'}</p>
        {error && <p role="alert">{error}</p>}
        <div className="assistant-card-actions"><button disabled={busy} onClick={() => setRemoving(null)}>取消</button>
          <button disabled={busy} className="assistant-danger" onClick={() => void mutate(() => lumenApi.editAssistant({
            action: removing.kind === 'collection' ? 'delete_collection' : removing.kind === 'goal' ? 'trash_goal' : 'trash_resource',
            id: removing.value.id, expected_revision: removing.value.revision,
          })).then(saved => {if (saved) {
            if (removing.kind === 'goal') {setGoalDetail(null); onNavigate?.('goals')}
            else if (removing.kind === 'resource') closeResource()
            else setFilter('all')
            setRemoving(null)
          }})}>{busy ? '正在删除…' : '确认删除'}</button></div>
      </Panel>}
      {creatingCollection && <Panel title="新建集合" onClose={() => setCreatingCollection(false)}>
        {error && <p role="alert">{error}</p>}
        <form className="assistant-form" onSubmit={e => {e.preventDefault(); void mutate(() => lumenApi.editAssistant({
          action: 'collection', id: collectionIdentity.current, title: collectionTitle,
        })).then(saved => {if (saved) {setCreatingCollection(false); setCollectionTitle(''); collectionIdentity.current = crypto.randomUUID()}})}}>
          <label>集合名称<input required maxLength={200} value={collectionTitle} onChange={e => setCollectionTitle(e.target.value)} /></label>
          <button disabled={busy} type="submit">创建集合</button>
        </form>
      </Panel>}
    </section>
  )
}

function Empty({ title, detail, action }: { title: string; detail: string; action?: React.ReactNode }) {
  return (
    <div className="assistant-empty">
      <img className="assistant-empty-fox" src={foxImage(0)} alt="" />
      <h3>{title}</h3>
      <p>{detail}</p>
      {action && <div className="assistant-empty-actions">{action}</div>}
    </div>
  )
}

function Panel({
  title,
  onClose,
  children,
  dirty = false,
  busy = false,
  className = '',
}: {
  title: string
  onClose: () => void
  children: React.ReactNode
  dirty?: boolean
  busy?: boolean
  className?: string
}) {
  const id = useId()
  const [discarding, setDiscarding] = useState(false)
  const close = () => { if (!busy) { if (dirty) setDiscarding(true); else onClose() } }
  const ref = useModalFocus(close)
  return (
    <div
      className="runtime-overlay assistant-overlay"
      ref={ref}
      role="dialog"
      aria-modal="true"
      aria-labelledby={id}
      tabIndex={-1}
    >
      <div className={`assistant-panel ${className}`}>
        <header>
          <h2 id={id}>{title}</h2>
          <button aria-label="关闭" disabled={busy} onClick={close}>
            <X size={22} />
          </button>
        </header>
        {discarding && <div className="assistant-notice" role="alert">
          还有未保存的修改。
          <button disabled={busy} onClick={() => setDiscarding(false)}>继续编辑</button>
          <button disabled={busy} onClick={onClose}>放弃草稿</button>
        </div>}
        {children}
      </div>
    </div>
  )
}

function AutomationEditor({
  definition,
  intent,
  busy,
  error = '',
  onClose,
  onSave,
  researchAvailable = false,
  linkedGoal,
}: {
  definition: AutomationDefinition | null
  intent: string
  busy: boolean
  error?: string
  onClose: () => void
  onSave: (spec: AutomationSpec, requestId: string) => Promise<void>
  researchAvailable?: boolean
  linkedGoal?: string
}) {
  const [profile, setProfile] = useState<'workspace_snapshot' | 'research_readonly'>(definition?.spec.profile ?? 'research_readonly')
  const [value, setValue] = useState({
    name: definition?.spec.name ?? '',
    intent: definition?.spec.intent ?? intent,
    paths: definition?.spec.paths.join('\n') ?? '',
  })
  const [schedule, setSchedule] = useState(
    definition?.spec.schedule ?? {
      kind: 'weekdays' as const,
      timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
      hour: 8,
      minute: 30,
      weekday: 4,
    },
  )
  const [limit, setLimit] = useState(definition?.spec.request_limit ?? 6)
  const [timeout, setTimeout] = useState(
    definition?.spec.timeout_seconds ?? 180,
  )
  const saveRequest = useRef({ payload: '', id: '' })
  const [endAt, setEndAt] = useState(
    definition?.spec.end_at
      ? new Date(definition.spec.end_at).toISOString().slice(0, 16)
      : '',
  )
  const submit = (event: FormEvent) => {
    event.preventDefault()
    const spec: AutomationSpec = {
      ...value,
      profile,
      context_refs: definition?.spec.context_refs ?? (linkedGoal ? [{kind: 'goal', id: linkedGoal}] : []),
      paths: (profile === 'workspace_snapshot' ? value.paths : '')
        .split('\n')
        .map((p) => p.trim())
        .filter(Boolean),
      schedule,
      request_limit: limit,
      timeout_seconds: timeout,
      notify: definition?.spec.notify ?? true,
      end_at: endAt ? new Date(endAt + 'Z').toISOString() : null,
    }
    const payload = JSON.stringify(spec)
    if (saveRequest.current.payload !== payload)
      saveRequest.current = { payload, id: crypto.randomUUID() }
    void onSave(spec, saveRequest.current.id)
  }
  return (
    <Panel title={definition ? '调整持续跟进' : '设置持续跟进'} onClose={onClose}>
      {error && (
        <p role="alert" className="settings-inline-error">
          {error}
        </p>
      )}
      <form onSubmit={submit} className="assistant-form">
        <label>
          名称
          <input
            required
            maxLength={160}
            value={value.name}
            onChange={(e) => setValue({ ...value, name: e.target.value })}
          />
        </label>
        <label>
          你希望 Lumen 做什么
          <textarea
            required
            maxLength={8000}
            value={value.intent}
            onChange={(e) => setValue({ ...value, intent: e.target.value })}
          />
        </label>
        <label>
          来源方式
          <select value={profile} onChange={e => setProfile(e.target.value as typeof profile)}>
            <option value="research_readonly">联网研究，无需文件</option>
            <option value="workspace_snapshot">整理选定工作区文件</option>
          </select>
        </label>
        {profile === 'research_readonly' && <p role="status">{researchAvailable ? '只使用已配置且明确允许的检索来源，结果送到 Lumen。' : '后台检索尚未配置或未获允许，可以先保存草稿；在设置中配置内建 web_search 与权限后才能成功试跑。'}</p>}
        {profile === 'workspace_snapshot' && <label>
          来源文件
          <textarea
            required
            value={value.paths}
            placeholder="README.md&#10;docs/weekly-notes.md"
            onChange={(e) => setValue({ ...value, paths: e.target.value })}
          />
          <small>工作区相对路径，每行一份。只读这些明确选择的文件。</small>
        </label>}
        <div className="assistant-form-row">
          <label>
            重复规则
            <select
              value={schedule.kind}
              onChange={(e) =>
                setSchedule({
                  ...schedule,
                  kind: e.target.value as typeof schedule.kind,
                })
              }
            >
              <option value="daily">每天</option>
              <option value="weekdays">工作日</option>
              <option value="weekly">每周</option>
            </select>
          </label>
          <label>
            时间
            <input
              required
              type="time"
              value={`${String(schedule.hour).padStart(2, '0')}:${String(schedule.minute).padStart(2, '0')}`}
              onChange={(e) => {
                const [hour, minute] = e.target.value.split(':').map(Number)
                setSchedule({ ...schedule, hour, minute })
              }}
            />
          </label>
        </div>
        {schedule.kind === 'weekly' && (
          <label>
            星期
            <select
              value={schedule.weekday}
              onChange={(e) =>
                setSchedule({ ...schedule, weekday: Number(e.target.value) })
              }
            >
              {['一', '二', '三', '四', '五', '六', '日'].map((label, n) => (
                <option key={n} value={n}>
                  星期{label}
                </option>
              ))}
            </select>
          </label>
        )}
        <details><summary>时区、执行预算与结束时间</summary>
        <label>
          IANA 时区
          <input
            required
            value={schedule.timezone}
            onChange={(e) =>
              setSchedule({ ...schedule, timezone: e.target.value })
            }
          />
        </label>
        <div className="assistant-form-row">
          <label>
            最多模型步骤
            <input
              type="number"
              min={1}
              max={50}
              required
              value={limit}
              onChange={(e) => setLimit(Number(e.target.value))}
            />
          </label>
          <label>
            执行时限（秒）
            <input
              type="number"
              min={10}
              max={1800}
              required
              value={timeout}
              onChange={(e) => setTimeout(Number(e.target.value))}
            />
          </label>
        </div>
        <label>
          结束时间（UTC，可选）
          <input
            type="datetime-local"
            value={endAt}
            onChange={(e) => setEndAt(e.target.value)}
          />
        </label>
        </details>
        <p>
          每个模型步骤可能重试，费用按实际用量计算。保存为未启用草稿。试跑成功后，你可以启用；编辑会使旧验证失效。
        </p>
        <button className="assistant-primary" disabled={busy} type="submit">
          保存草稿
        </button>
      </form>
    </Panel>
  )
}

function GoalExecutionEditor({goal, busy, error, onClose, onSave}: {
  goal: AssistantGoal; busy: boolean; error: string; onClose: () => void
  onSave: (criteria: string, turnLimit: number | null, requestId: string) => Promise<void>
}) {
  const [criteria, setCriteria] = useState(goal.execution?.criteria ?? '')
  const [limit, setLimit] = useState(goal.execution?.turn_limit?.toString() ?? '')
  const requestId = useRef(crypto.randomUUID())
  const resuming = !!goal.execution && !['review', 'accepted'].includes(goal.execution.status)
  const used = resuming ? goal.execution?.turns ?? 0 : 0
  const valid = criteria.trim() && (!limit || (Number.isInteger(Number(limit)) && Number(limit) > used && Number(limit) <= 100))
  return <Panel title={resuming ? '恢复目标推进' : '启动目标推进'} busy={busy} onClose={onClose}
    dirty={criteria !== (goal.execution?.criteria ?? '') || limit !== (goal.execution?.turn_limit?.toString() ?? '')}>
    <form className="assistant-form" onSubmit={e => {e.preventDefault(); if (valid && !busy) void onSave(criteria.trim(), limit ? Number(limit) : null, requestId.current)}}>
      <p>围绕「{goal.title}」执行、核验并决定下一步。达到标准后提交你验收，遇到阻塞会停止。</p>
      <label>怎样才算达成<textarea required maxLength={8000} disabled={busy} value={criteria} onChange={e => setCriteria(e.target.value)} placeholder="写下可检查的结果、证据和必须保留的约束" /></label>
      <label>推进轮数上限（可选）<input type="number" min={used + 1} max={100} step={1} disabled={busy} value={limit} onChange={e => setLimit(e.target.value)} placeholder="留空不设置轮数上限" /></label>
      <p>已使用 {used} 轮。每轮可包含多次模型请求与工具调用，原有权限和运行预算继续生效。可随时暂停；服务重启后需明确恢复。</p>
      {error && <p role="alert">{error}</p>}
      <button type="submit" className="assistant-primary" disabled={busy || !valid}>{busy ? '正在启动…' : resuming ? '恢复推进' : '启动推进'}</button>
    </form>
  </Panel>
}

function GoalEditor({
  busy,
  error = '',
  onClose,
  onSave,
  initialGoal,
  initialTitle = '',
}: {
  busy: boolean
  error?: string
  onClose: () => void
  onSave: (
    goal: Pick<AssistantGoal, 'title' | 'intent' | 'status' | 'milestones' | 'automation_ids'>,
    requestId: string,
  ) => Promise<void>
  initialGoal?: AssistantGoal
  initialTitle?: string
}) {
  const [title, setTitle] = useState(initialGoal?.title ?? initialTitle.slice(0, 200))
  const [intent, setIntent] = useState(initialGoal?.intent ?? '')
  const [milestones, setMilestones] = useState(initialGoal?.milestones ?? [])
  const requestId = useRef(crypto.randomUUID())
  const pendingMilestoneFocus = useRef<string | null>(null)
  const valid = title.trim() && milestones.every(m => m.title.trim())
  return (
    <Panel title={initialGoal ? '编辑目标' : '新建目标'} onClose={onClose} busy={busy} className="goal-editor-panel"
      dirty={title !== (initialGoal?.title ?? initialTitle.slice(0, 200)) || intent !== (initialGoal?.intent ?? '') ||
        JSON.stringify(milestones) !== JSON.stringify(initialGoal?.milestones ?? [])}>
      <form
        className="assistant-form goal-editor-form"
        onSubmit={(e) => {
          e.preventDefault()
          if (busy || !valid) return
          void onSave(
            {
              title: title.trim(),
              intent: intent.trim(),
              status: initialGoal?.status ?? 'active',
              milestones: milestones.map(m => ({...m, title: m.title.trim()})),
              automation_ids: initialGoal?.automation_ids ?? [],
            },
            requestId.current,
          )
        }}
      >
        <p>{initialGoal ? '修改想达成的结果与已经确认的里程碑。' : '想完成什么？先告诉 Lumen，一起把第一步想清楚。'}</p>
        <fieldset disabled={busy} className="goal-editor-fields">
        <label>
          想达成什么
          <input
            required
            maxLength={200}
            placeholder="例如：完成产品体验改版"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          补充说明（可选）
          <textarea maxLength={8000} value={intent} onChange={e => setIntent(e.target.value)} placeholder="你关心什么，怎样的结果对你有帮助" />
        </label>
        {initialGoal && <section className="goal-editor-plan" aria-label="编辑里程碑">
          <h3>里程碑</h3><p>写下可确认的结果，完成后由你勾选。最多 30 项。</p>
          {!milestones.length && <p>还没有里程碑，可以稍后与 Lumen 讨论后添加。</p>}
          {milestones.map((m, index) => <div className="goal-editor-milestone" key={m.id}>
            <input type="checkbox" aria-label={`里程碑 ${index + 1} 已完成`} checked={m.done}
              onChange={e => setMilestones(items => items.map(item => item.id === m.id ? {...item, done: e.target.checked} : item))} />
            <input aria-label={`里程碑 ${index + 1}`} required maxLength={300} value={m.title}
              ref={element => {if (element && pendingMilestoneFocus.current === m.id) {element.focus(); pendingMilestoneFocus.current = null}}}
              onChange={e => setMilestones(items => items.map(item => item.id === m.id ? {...item, title: e.target.value} : item))} />
            <button type="button" aria-label={`删除里程碑 ${index + 1}`} onClick={() => setMilestones(items => items.filter(item => item.id !== m.id))}><Trash size={17} /></button>
          </div>)}
          <button type="button" disabled={milestones.length >= 30} onClick={() => {
            const id = crypto.randomUUID(); pendingMilestoneFocus.current = id
            setMilestones(items => [...items, {id, title: '', done: false}])
          }}><Plus size={16} />添加里程碑</button>
        </section>}
        </fieldset>
        {error && <p role="alert" className="goal-editor-error">{error}</p>}
        <footer className="goal-editor-footer">
          <p>{initialGoal ? '成果文件会自动关联，目标完成由你确认。' : '创建后进入关联对话开始规划。持续推进与定时跟进由你另外启动。'}</p>
          <button className="assistant-primary" type="submit" disabled={busy || !valid}>{busy ? '正在保存…' : initialGoal ? '保存修改' : '创建并规划'}</button>
        </footer>
      </form>
    </Panel>
  )
}

function NoteEditor({
  busy,
  error = '',
  initialTitle = '',
  initialContent = '',
  onClose,
  onSave,
}: {
  busy: boolean
  error?: string
  initialTitle?: string
  initialContent?: string
  onClose: () => void
  onSave: (title: string, content: string, requestId: string) => Promise<void>
}) {
  const [title, setTitle] = useState(initialTitle)
  const [content, setContent] = useState(initialContent)
  const requestId = useRef(crypto.randomUUID())
  return (
    <Panel title={initialTitle ? '编辑资料' : '新建页面'} onClose={onClose} dirty={title !== initialTitle || content !== initialContent}>
      {error && <p role="alert">{error}</p>}
      <form
        className="assistant-form"
        onSubmit={(e) => {
          e.preventDefault()
          void onSave(title, content, requestId.current)
        }}
      >
        <label>
          标题
          <input
            required
            maxLength={200}
            value={title}
            onChange={(e) => setTitle(e.target.value)}
          />
        </label>
        <label>
          内容
          <textarea
            maxLength={100000}
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
        </label>
        <button className="assistant-primary" disabled={busy}>
          保存到资料空间
        </button>
      </form>
    </Panel>
  )
}
