'use client'

import {
  Archive,
  ArrowDown,
  ArrowCounterClockwise,
  ArrowsClockwise,
  BookOpen,
  CaretDown,
  Check,
  CheckCircle,
  CircleNotch,
  ClockCounterClockwise,
  Copy,
  DotsThree,
  FolderSimple,
  FolderOpen,
  GearSix,
  Globe,
  HardDrives,
  Info,
  Images,
  List,
  ListChecks,
  ListMagnifyingGlass,
  MagnifyingGlass,
  NotePencil,
  PencilSimpleLine,
  PlugsConnected,
  Plus,
  Robot,
  ShieldCheck,
  SidebarSimple,
  Sparkle,
  TerminalWindow,
  TreeStructure,
  Trash,
  WarningCircle,
  Wrench,
  X,
} from '@phosphor-icons/react'
import { useCallback, useEffect, useId, useMemo, useReducer, useRef, useState, type MouseEvent } from 'react'
import { exchangeLaunchToken, lumenApi, subscribeRun } from '@/lib/api/client'
import { AuthenticationError } from '@/lib/api/errors'
import type {
  AgentRecord,
  AssistantResource,
  ApprovalMode,
  PermissionPreset,
  SandboxMode,
  FilesystemScope,
  AttachmentRef,
  Bootstrap,
  CapabilityInventory,
  CheckpointRecord,
  ConfigurationSnapshot,
  ConfiguredModel,
  EventEnvelope,
  QueueMode,
  ReasoningLevel,
  ReasoningSelection,
  SessionSummary,
  SessionSnapshot,
  TimelineEntry,
  TurnRestorePreview,
} from '@/lib/api/types'
import { initialRunState, runReducer } from '@/lib/state/run-reducer'
import {
  buildSlashCommands,
  parsePromptInvocation,
} from '@/lib/slash-commands'
import { Composer } from './composer'
import { useAppearance } from './appearance-control'
import { AssistantWorkspace, assistantPages, goalStatusLabels, goalExecutionLabels, type AssistantPage, type AssistantDiscussion, type AssistantView } from './assistant-workspace'
import type { AssistantContextRef, AssistantIdea } from '@/lib/api/types'
import { PersonalPreferences } from './personal-preferences'
import { ClarificationPrompt } from './clarification-prompt'
import { CopyButton } from './copy-button'
import { UserMessage } from './user-message'
import { ConversationScrollNav } from './conversation-scroll-nav'
import { ChoiceMenu, type ChoiceOption } from './choice-menu'
import { LandingEntry } from './landing-entry'
import { LiveVoiceControls } from './live-voice-controls'
import { LumenLogo, LumenMark } from './lumen-logo'
import { MarkdownMessage } from './markdown-message'
import { ReportReadingButton } from './markdown-report'
import { PlanPanel, PlanProgress, PlanProposal, PlanReview } from './plan-panel'
import { DocumentPreview, DocumentProvider, DocumentResults } from './document-preview'
import { resourceTypeLabel } from './resource-icon'
import { SessionActionsMenu } from './session-actions-menu'
import { SessionSearchDialog } from './session-search-dialog'
import { ThinkingOrb } from './thinking-orb'
import { ToolOutputDetails } from './tool-output'
import { TurnSourcesProvider, TurnSourcesSummary } from './turn-sources'
import { EMPTY_WORKSPACE_PROMPT } from '@/lib/copy'
import { projectThinkingMarkup } from '@/lib/thinking-markup'
import {
  activityMeta,
  activityDuration,
  activityTitle,
  isRunningTool,
  readableActivity,
  transcriptEventLabel,
  timelineNoteLabel,
  toolSourceLabel,
  toolActivityLabel,
  turnPresentation,
  type TurnPresentation,
} from '@/lib/turn-activity'
import { useModalFocus } from './use-modal-focus'
import { ModelSettingsControl, modelDisplayName, type ModelSettingsView } from './model-settings-control'

function requestId() {
  return crypto.randomUUID()
}

const permissionChoices: Array<ChoiceOption<PermissionPreset | 'custom'>> = [
  { value: 'manual', label: '每次确认', description: '仅限工作区。读取通常免确认，修改和执行按规则确认。' },
  { value: 'accept_edits', label: '自动改文件', description: '仅限工作区。文件修改自动允许，其他动作按规则确认。' },
  { value: 'workspace_auto', label: '工作区内自动执行', compactLabel: '工作区自动', description: '在工作区沙箱内自动执行常规操作，命令联网沿用配置。' },
  { value: 'full_access', label: '完全访问', description: '可操作本机其他目录、命令可联网。内建提交、推送仍每次确认。' },
]

function reasoningOptions(selection?: ReasoningSelection | null, requested = selection?.requested) {
  return (selection?.supported_levels ?? ['provider_default'] as ReasoningLevel[])
    .filter((level) => !selection?.level_map?.[level] || selection.level_map[level] === level
      || !selection.supported_levels.includes(selection.level_map[level])
      || level === requested)
    .map((level) => ({
      value: level,
      compactLabel: level === 'provider_default'
        ? selection?.provider_default_level ? `默认 · ${selection.provider_default_level}` : '供应商默认'
        : undefined,
      label: level === 'provider_default'
        ? selection?.provider_default_level
          ? `跟随供应商默认（${selection.provider_default_level}）` : '跟随供应商默认'
        : selection?.level_map?.[level] && selection.level_map[level] !== level
          ? `${level} → ${selection.level_map[level]}` : level,
    }))
}

export function LumenApp() {
  const appearance = useAppearance()
  const [assistantPage, setAssistantPage] = useState<AssistantPage>('chat')
  const [automationIntent, setAutomationIntent] = useState('')
  const [goalSourceSessionId, setGoalSourceSessionId] = useState<string | undefined>()
  const [assistantObjectId, setAssistantObjectId] = useState<string | undefined>()
  const [assistantContext, setAssistantContext] = useState<AssistantContextRef[]>([])
  const [contextResource, setContextResource] = useState<Awaited<ReturnType<typeof lumenApi.assistantResource>> | null>(null)
  const [contextTab, setContextTab] = useState<'content' | 'discussion'>('discussion')
  const [contextFile, setContextFile] = useState<AssistantResource | null>(null)
  useEffect(() => setContextFile(null), [contextResource?.resource.id, contextResource?.resource.revision])
  const [assistantNotice, setAssistantNotice] = useState('')
  const [publishing, setPublishing] = useState(false)
  const [sessionFiles, setSessionFiles] = useState<AssistantResource[]>([])
  const [contextGoal, setContextGoal] = useState<Awaited<ReturnType<typeof lumenApi.assistantGoal>>['goal'] | null>(null)
  const [approvalSubmitting, setApprovalSubmitting] = useState(false)
  const [approvalError, setApprovalError] = useState('')
  const approvalRequestRef = useRef(false)
  const discussionDrafts = useRef(new Map<string, string>())
  const assistantViews = useRef(new Map<string, AssistantView>())
  const navigateAssistant = useCallback((page: AssistantPage, id?: string) => {
    setAssistantPage(page); setAssistantObjectId(id); setAutomationIntent(''); setGoalSourceSessionId(undefined); setSidebarOpen(false)
    setAssistantNotice('')
    const url = new URL(window.location.href)
    url.searchParams.set('view', page === 'goals' && id ? 'goal' : page === 'library' && id ? 'resource' : page)
    if (id) url.searchParams.set('id', id); else url.searchParams.delete('id')
    if (url.href !== window.location.href) window.history.pushState({}, '', url.pathname + url.search)
  }, [])
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [sessionView, setSessionView] = useState<'active' | 'archived'>('active')
  const [sessionMenu, setSessionMenu] = useState<{
    session: SessionSummary
    anchor: HTMLButtonElement
  } | null>(null)
  const [sessionDialog, setSessionDialog] = useState<{
    mode: 'rename' | 'delete'
    session: SessionSummary
  } | null>(null)
  const [sessionManagementBusy, setSessionManagementBusy] = useState(false)
  const [run, dispatch] = useReducer(runReducer, initialRunState)
  const [input, setInput] = useState('')
  useEffect(() => { discussionDrafts.current.set(sessionId ?? 'new', input) }, [sessionId, input])
  const [attachments, setAttachments] = useState<AttachmentRef[]>([])
  const [queueMode, setQueueMode] = useState<QueueMode>('steer')
  const [loading, setLoading] = useState(true)
  const [authenticationRequired, setAuthenticationRequired] = useState(false)
  const [connectionError, setConnectionError] = useState('')
  const [connectionAttempt, setConnectionAttempt] = useState(0)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [sidebarCollapsed, setSidebarCollapsed] = useState(false)
  const [sessionSearchOpen, setSessionSearchOpen] = useState(false)
  const sessionSearchReturnFocusRef = useRef<HTMLButtonElement>(null)
  const sidebarToggleRef = useRef<HTMLButtonElement>(null)
  const sidebarRailRef = useRef<HTMLElement>(null)
  const sidebarFocusRef = useModalFocus(() => setSidebarOpen(false), sidebarOpen)

  const setDesktopSidebarCollapsed = useCallback((collapsed: boolean) => {
    setSidebarCollapsed(collapsed)
    try { localStorage.setItem('lumen.sidebarCollapsed', String(collapsed)) } catch { /* Storage may be disabled. */ }
  }, [])
  const expandSidebar = () => {
    setSessionMenu(null)
    const mobile = window.matchMedia?.('(max-width: 800px)').matches ?? false
    if (mobile) setSidebarOpen(true)
    else setDesktopSidebarCollapsed(false)
    window.requestAnimationFrame(() => {
      if (!mobile) sidebarFocusRef.current?.querySelector<HTMLButtonElement>('.sidebar-close')?.focus()
    })
  }
  const openSessionSearch = (event: MouseEvent<HTMLButtonElement>) => {
    sessionSearchReturnFocusRef.current = window.matchMedia?.('(max-width: 800px)').matches
      ? sidebarToggleRef.current : event.currentTarget
    setSessionMenu(null)
    setSidebarOpen(false)
    setSessionSearchOpen(true)
  }
  const collapseSidebar = () => {
    setSessionMenu(null)
    if (window.matchMedia?.('(max-width: 800px)').matches) setSidebarOpen(false)
    else {
      setDesktopSidebarCollapsed(true)
      window.requestAnimationFrame(() => sidebarRailRef.current?.querySelector('button')?.focus())
    }
  }
  useEffect(() => {
    try { setSidebarCollapsed(localStorage.getItem('lumen.sidebarCollapsed') === 'true') } catch { /* Optional preference. */ }
    const media = window.matchMedia?.('(max-width: 800px)')
    const changed = () => { setSidebarOpen(false); setSessionMenu(null) }
    media?.addEventListener('change', changed)
    return () => media?.removeEventListener('change', changed)
  }, [])
  const [confirmAuto, setConfirmAuto] = useState<string | null>(null)
  const [confirmPreset, setConfirmPreset] = useState<PermissionPreset | null>(null)
  const [autoError, setAutoError] = useState('')
  const [approvalChanging, setApprovalChanging] = useState(false)
  const approvalChangeRef = useRef(false)
  const [planFeedback, setPlanFeedback] = useState('')
  const [planReviewBusy, setPlanReviewBusy] = useState(false)
  const planReviewRequest = useRef<symbol | null>(null)
  useEffect(() => { setPlanFeedback('') }, [sessionId, run.planReviewRevision])
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [checkpointsOpen, setCheckpointsOpen] = useState(false)
  const [transcriptOpen, setTranscriptOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [modelSettingsView, setModelSettingsView] = useState<ModelSettingsView>(null)
  const [reasoning, setReasoning] = useState<ReasoningSelection | null>(null)
  const [modeMenuOpen, setModeMenuOpen] = useState(false)
  const [modelChanging, setModelChanging] = useState(false)
  const modelChangeRef = useRef(false)
  const queuedModelRef = useRef<string | null>(null)
  const [settingsNotice, setSettingsNotice] = useState('')
  useEffect(() => {
    const requested = queuedModelRef.current
    queuedModelRef.current = bootstrap?.pendingModel ?? null
    if (requested && bootstrap && !bootstrap.pendingModel) {
      const currentModel = modelDisplayName(bootstrap.modelId, bootstrap.activeModel)
      setSettingsNotice(bootstrap.activeModel === requested
        ? `已切换到 ${currentModel}，用于下一轮对话。`
        : `未能切换到 ${requested}，仍在使用 ${currentModel}。请重新选择模型重试。`)
    }
  }, [bootstrap?.pendingModel, bootstrap?.activeModel, bootstrap?.modelId])
  const [checkpoints, setCheckpoints] = useState<CheckpointRecord[]>([])
  const [controlBusy, setControlBusy] = useState(false)
  const [stopping, setStopping] = useState(false)
  const [stopError, setStopError] = useState('')
  const stoppingRef = useRef<string | null>(null)
  const editingRef = useRef(false)
  const submitRequestRef = useRef(false)
  const [messageEditing, setMessageEditing] = useState(false)
  const selectedSessionRef = useRef<string | null>(null)
  const editRequestRef = useRef<{
    key: string; runRequestId: string
  } | null>(null)
  const closeStreamRef = useRef<(() => void) | null>(null)
  const timelineRef = useRef<HTMLElement>(null)
  const stickToLatestRef = useRef(true)
  const sessionListRevision = useRef(0)

  const refreshSessions = useCallback(async () => {
    const revision = ++sessionListRevision.current
    const items = await lumenApi.listSessions(true)
    if (revision === sessionListRevision.current) setSessions(items)
  }, [])

  const refreshChrome = useCallback(async () => {
    const [nextBootstrap] = await Promise.all([
      lumenApi.bootstrap(),
      refreshSessions(),
    ])
    setBootstrap(nextBootstrap)
    return nextBootstrap
  }, [refreshSessions])

  useEffect(() => {
    if (!bootstrap?.activeRunId || bootstrap.activeRunId === run.runId) return
    const timer = setInterval(() => { void refreshChrome().catch(() => {}) }, 1500)
    return () => clearInterval(timer)
  }, [bootstrap?.activeRunId, run.runId, refreshChrome])

  const pendingTitles = sessions.filter((item) => item.titlePending).map((item) => item.sessionId).join(',')
  useEffect(() => {
    if (!pendingTitles) return
    let disposed = false
    let timer: ReturnType<typeof setTimeout>
    const poll = async () => {
      try { if (!document.hidden) await refreshSessions() } catch { /* Retry metadata independently of the run. */ }
      if (!disposed) timer = setTimeout(poll, 1500)
    }
    timer = setTimeout(poll, 1500)
    return () => { disposed = true; clearTimeout(timer) }
  }, [pendingTitles, refreshSessions])

  const attachRun = useCallback(
    (runId: string) => {
      closeStreamRef.current?.()
      closeStreamRef.current = subscribeRun(
        runId,
        (event: EventEnvelope) => {
          if (selectedSessionRef.current !== event.sessionId) return
          dispatch({ type: 'event', event })
          if (
            event.type === 'run.completed' ||
            event.type === 'run.waiting_for_user' ||
            event.type === 'run.failed' ||
            event.type === 'run.cancelled'
          ) {
            closeStreamRef.current = null
            void refreshChrome()
            const selected = selectedSessionRef.current
            if (selected) void lumenApi.session(selected).then((snapshot) => {
              if (selectedSessionRef.current !== selected) return
              dispatch({ type: 'work-state-refreshed', workProducts: snapshot.workProducts,
                pendingEffects: snapshot.pendingEffects, recoverableEffects: snapshot.recoverableEffects })
            }).catch(() => { /* The runtime inspector can retry the read. */ })
          }
        },
        (message) => dispatch({ type: 'local-error', message }),
      )
    },
    [refreshChrome],
  )

  const applySessionSnapshot = useCallback((snapshot: SessionSnapshot) => {
    setReasoning(snapshot.reasoning ?? null)
    selectedSessionRef.current = snapshot.sessionId
    setSessionId(snapshot.sessionId)
    dispatch({ type: 'snapshot', snapshot })
    const url = new URL(window.location.href)
    url.searchParams.set('session', snapshot.sessionId)
    url.searchParams.delete('view'); url.searchParams.delete('id')
    window.history.replaceState({}, '', `${url.pathname}${url.search}`)
  }, [])

  const openSession = useCallback(
    async (id: string) => {
      setAssistantPage('chat')
      setAssistantContext([]); setContextResource(null); setAssistantNotice('')
      setContextTab('discussion')
      selectedSessionRef.current = id
      setConfirmAuto(null)
      setAutoError('')
      planReviewRequest.current = null
      setPlanReviewBusy(false)
      setPlanFeedback('')
      setSettingsNotice('')
      setSessionMenu(null)
      closeStreamRef.current?.()
      closeStreamRef.current = null
      stickToLatestRef.current = true
      setShowJumpToLatest(false)
      setLoading(true)
      try {
        const snapshot = await lumenApi.session(id)
        if (selectedSessionRef.current !== id) return
        applySessionSnapshot(snapshot)
        if (lumenApi.assistantContext) void lumenApi.assistantContext(id).then(value => {
          if (selectedSessionRef.current !== id) return
          setAssistantContext(value.context_refs)
          const resourceRef = value.context_refs.find(r => r.kind === 'resource')
          if (resourceRef) void lumenApi.assistantResource(resourceRef.id, false, resourceRef.revision ?? undefined).then(resource => {
            if (selectedSessionRef.current === id) setContextResource(resource)
          }).catch(() => {
            if (selectedSessionRef.current === id) setAssistantNotice('关联资料暂不可用，可回资料空间重新选择。')
          })
        }).catch(() => { /* Ordinary sessions do not depend on assistant storage. */ })
        if (snapshot.activeRunId) attachRun(snapshot.activeRunId)
      } catch (error) {
        if (error instanceof AuthenticationError) {
          setAuthenticationRequired(true)
          return
        }
        dispatch({
          type: 'local-error',
          message: error instanceof Error ? error.message : '无法恢复会话',
        })
      } finally {
        if (selectedSessionRef.current === id) { setLoading(false); setSidebarOpen(false) }
      }
    },
    [applySessionSnapshot, attachRun],
  )

  useEffect(() => {
    const restore = () => {
      setAssistantNotice(''); setContextTab('discussion')
      const url = new URL(window.location.href)
      const view = url.searchParams.get('view')
      const page = view === 'goal' ? 'goals' : view === 'resource' ? 'library' : view
      if (assistantPages.some(p => p.id === page)) {
        setAssistantPage(page as AssistantPage)
        setAssistantObjectId(url.searchParams.get('id') ?? undefined)
      } else {
        setAssistantPage('chat')
        const id = url.searchParams.get('session')
        if (id && id !== selectedSessionRef.current) void openSession(id)
      }
    }
    restore(); window.addEventListener('popstate', restore)
    return () => window.removeEventListener('popstate', restore)
  }, [openSession])

  useEffect(() => {
    const requireAuthentication = () => {
      setAuthenticationRequired(true)
      closeStreamRef.current?.()
    }
    window.addEventListener('lumen:authentication-required', requireAuthentication)
    return () => window.removeEventListener('lumen:authentication-required', requireAuthentication)
  }, [])

  useEffect(() => {
    let disposed = false
    const initialise = async () => {
      setLoading(true)
      try {
        await exchangeLaunchToken()
        const [nextBootstrap, nextSessions] = await Promise.all([
          lumenApi.bootstrap(),
          lumenApi.listSessions(true),
        ])
        if (disposed) return
        setAuthenticationRequired(false)
        setConnectionError('')
        setBootstrap(nextBootstrap)
        setSessions(nextSessions)
        const requested = new URL(window.location.href).searchParams.get('session')
        const view = new URL(window.location.href).searchParams.get('view')
        if (requested && (!view || view === 'chat')) await openSession(requested)
      } catch (error) {
        if (!disposed) {
          if (error instanceof AuthenticationError) setAuthenticationRequired(true)
          else setConnectionError(error instanceof TypeError ? '请确认 Lumen 服务正在运行，然后重试。'
            : error instanceof Error ? error.message : '无法连接 Lumen Web')
        }
      } finally {
        if (!disposed) setLoading(false)
      }
    }
    void initialise()
    return () => {
      disposed = true
      closeStreamRef.current?.()
    }
  }, [openSession, connectionAttempt])

  const createSession = useCallback(async () => {
    const created = await lumenApi.createSession()
    await refreshChrome()
    await openSession(created.sessionId)
    return created.sessionId
  }, [openSession, refreshChrome])

  const updateSessionSettings = useCallback(async (
    targetSession: string,
    settings: Parameters<typeof lumenApi.updateSessionSettings>[1],
  ) => {
    let result: Record<string, unknown>
    try {
      result = await lumenApi.updateSessionSettings(targetSession, settings)
      if (settings.permissionPreset && (
        !['manual', 'accept_edits', 'auto'].includes(String(result.approval_mode))
        || !['workspace_write', 'disabled'].includes(String(result.sandbox_mode))
      )) throw new Error('权限更新结果不完整，请刷新当前对话后重试。')
    } catch (error) {
      if (!settings.permissionPreset) throw error
      let snapshot: SessionSnapshot
      try { snapshot = await lumenApi.session(targetSession) }
      catch {
        if (selectedSessionRef.current === targetSession) {
          setConfirmAuto(null)
          setConnectionError('权限切换结果尚未核实，请重新连接并读取当前对话后再执行。')
        }
        throw error
      }
      if (selectedSessionRef.current === targetSession) applySessionSnapshot(snapshot)
      const expectedApproval = ['workspace_auto', 'full_access'].includes(settings.permissionPreset)
        ? 'auto' : settings.permissionPreset
      const expectedSandbox = settings.permissionPreset === 'full_access' ? 'disabled' : 'workspace_write'
      if (snapshot.approvalMode !== expectedApproval || snapshot.sandboxMode !== expectedSandbox) throw error
      result = { approval_mode: snapshot.approvalMode, sandbox_mode: snapshot.sandboxMode,
        filesystem_scope: snapshot.filesystemScope }
    }
    const applied = settings.permissionPreset ? {
      approvalMode: result.approval_mode as ApprovalMode,
      sandboxMode: result.sandbox_mode as SandboxMode,
      filesystemScope: (result.filesystem_scope ?? (result.sandbox_mode === 'disabled' ? 'full' : 'workspace')) as FilesystemScope,
    } : settings
    // Workspace bootstrap contains defaults, never the selected Session's modes.
    dispatch({ type: 'session-settings', sessionId: targetSession, settings: applied })
    if (selectedSessionRef.current === targetSession && Array.isArray(result.resolved_call_ids)) {
      dispatch({ type: 'approval-decided', callIds: result.resolved_call_ids as string[], approved: true })
    }
  }, [applySessionSnapshot])

  const setApprovalMode = useCallback(async (mode: ApprovalMode, resetCollaboration = false) => {
    if (approvalChangeRef.current) {
      throw new Error('正在更新审批模式，请稍候。')
    }
    const targetSession = sessionId ?? (await createSession())
    setAutoError('')
    setConfirmPreset(null)
    if (mode === 'auto') {
      setConfirmAuto(targetSession)
      return
    }
    setConfirmAuto(null)
    approvalChangeRef.current = true
    setApprovalChanging(true)
    try {
      await updateSessionSettings(targetSession, {
        approvalMode: mode,
        ...(resetCollaboration && !run.runId ? { collaborationMode: 'default' as const } : {}),
      })
    } finally { approvalChangeRef.current = false; setApprovalChanging(false) }
  }, [createSession, run.runId, sessionId, updateSessionSettings])

  const setPermissionPreset = useCallback(async (preset: PermissionPreset | 'custom') => {
    if (preset === 'custom') return
    if (approvalChangeRef.current) throw new Error('正在更新权限，请稍候。')
    if (bootstrap?.activeRunId || run.runId) throw new Error('请先停止运行，再切换访问范围。')
    const targetSession = sessionId ?? (await createSession())
    setAutoError('')
    if (preset === 'workspace_auto' || preset === 'full_access') {
      setConfirmPreset(preset)
      setConfirmAuto(targetSession)
      return
    }
    setConfirmAuto(null)
    approvalChangeRef.current = true
    setApprovalChanging(true)
    try {
      await updateSessionSettings(targetSession, { permissionPreset: preset })
    } finally { approvalChangeRef.current = false; setApprovalChanging(false) }
  }, [bootstrap?.activeRunId, createSession, run.runId, sessionId, updateSessionSettings])

  const setCollaborationMode = useCallback(async (mode: 'default' | 'plan') => {
    if (bootstrap?.activeRunId || run.runId) throw new Error('请先停止运行，再切换工作方式。')
    const targetSession = sessionId ?? (await createSession())
    await updateSessionSettings(targetSession, { collaborationMode: mode })
  }, [bootstrap?.activeRunId, createSession, run.runId, sessionId, updateSessionSettings])

  const refreshSessionCapabilities = useCallback(async () => {
    if (!sessionId) return
    const [snapshot, agents] = await Promise.all([
      lumenApi.session(sessionId),
      lumenApi.listAgents(sessionId),
    ])
    if (selectedSessionRef.current !== sessionId) return
    dispatch({ type: 'agents-refreshed', agents })
    dispatch({
      type: 'work-state-refreshed',
      workProducts: snapshot.workProducts,
      pendingEffects: snapshot.pendingEffects,
      recoverableEffects: snapshot.recoverableEffects,
    })
  }, [sessionId])

  const openInspector = useCallback(async () => {
    setInspectorOpen(true)
    await refreshSessionCapabilities()
  }, [refreshSessionCapabilities])

  const runAgentAction = useCallback(async (agent: AgentRecord, action: string) => {
    // TODO(设计审计 W6)：window.prompt 原生弹窗无焦点管理、样式不可控，后续替换为自绘弹层
    let payload: Record<string, unknown> = {}
    if (action === 'send_message') {
      const message = window.prompt(`给 ${agent.path ?? agent.id} 发送补充信息`)
      if (!message?.trim()) return
      payload = { message: message.trim() }
    } else if (action === 'continue') {
      const task = window.prompt(`继续 ${agent.path ?? agent.id} 的任务`)
      if (!task?.trim()) return
      payload = { task: task.trim() }
    } else if (action === 'close') {
      const reason = window.prompt('关闭 Agent 的处理说明')
      if (!reason?.trim()) return
      payload = { resolution: 'web_resolution', reason: reason.trim() }
    }
    setControlBusy(true)
    try {
      await lumenApi.agentAction(agent.id, action, payload)
      await refreshSessionCapabilities()
    } catch (error) {
      if (error instanceof AuthenticationError) return
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : 'Agent 操作失败',
      })
    } finally {
      setControlBusy(false)
    }
  }, [refreshSessionCapabilities])

  const openCheckpoints = useCallback(async () => {
    if (!sessionId) return
    setCheckpoints(await lumenApi.listCheckpoints(sessionId))
    setCheckpointsOpen(true)
  }, [sessionId])

  const forkCheckpoint = useCallback(async (checkpoint: CheckpointRecord) => {
    // TODO(设计审计 W6)：window.confirm 同上，后续替换为自绘确认弹层
    if (!sessionId || !window.confirm(`从 turn ${checkpoint.index + 1} 创建新任务分支？`)) return
    setControlBusy(true)
    try {
      const created = await lumenApi.forkSession(sessionId, checkpoint.index)
      await refreshChrome()
      setCheckpointsOpen(false)
      await openSession(created.sessionId)
    } finally {
      setControlBusy(false)
    }
  }, [openSession, refreshChrome, sessionId])

  const toggleTranscriptDensity = useCallback(async () => {
    if (!sessionId) return
    const transcriptDensity = run.transcriptDensity === 'normal' ? 'verbose' : 'normal'
    await lumenApi.updateSessionSettings(sessionId, { transcriptDensity })
    dispatch({ type: 'transcript-density', density: transcriptDensity })
  }, [run.transcriptDensity, sessionId])

  const dequeueInputs = useCallback(async () => {
    if (!run.runId) return
    const result = await lumenApi.dequeueInputs(run.runId)
    if (result.items.length) {
      setInput((current) => [result.items.map((item) => item.text).join('\n\n'), current]
        .filter((item) => item.trim()).join('\n\n'))
    }
  }, [run.runId])

  const waiveEffect = useCallback(async (effect: Record<string, unknown>, reason: string) => {
    if (!sessionId || run.runId || bootstrap?.activeRunId) throw new Error('请先停止运行再核实结果。')
    const id = String(effect.id ?? '')
    if (!id || !reason.trim()) throw new Error('请填写此操作的核实依据。')
    await lumenApi.waiveVerification(sessionId, [id], reason.trim())
    await refreshSessionCapabilities()
  }, [refreshSessionCapabilities, sessionId, run.runId, bootstrap?.activeRunId])

  const selectModel = useCallback(async (model: string) => {
    if (modelChangeRef.current) {
      throw new Error('正在更新模型，请稍候。')
    }
    if (model === (bootstrap?.pendingModel ?? bootstrap?.activeModel)) return
    if (!bootstrap?.availableModels.includes(model)) throw new Error(`未配置的模型：${model}`)
    modelChangeRef.current = true
    setModelChanging(true)
    setSettingsNotice('')
    try {
      const result = await lumenApi.updateWorkspaceSettings({ model })
      queuedModelRef.current = typeof result.pending_model === 'string' ? result.pending_model : null
      if (!result.pending_model) setReasoning(null)
      const nextBootstrap = await refreshChrome()
      if (!result.pending_model) {
        setSettingsNotice(`已切换到 ${modelDisplayName(nextBootstrap.modelId, model)}，用于下一轮对话。`)
      }
      const target = selectedSessionRef.current
      if (target) {
        const snapshot = await lumenApi.session(target)
        if (selectedSessionRef.current === target) setReasoning(snapshot.reasoning ?? null)
      }
    } finally {
      modelChangeRef.current = false
      setModelChanging(false)
    }
  }, [bootstrap, refreshChrome])

  const selectThinking = useCallback(async (level: string) => {
    if (level === 'configured') return
    if (modelChangeRef.current || bootstrap?.pendingModel) {
      throw new Error('请等待模型切换完成后再设置推理强度。')
    }
    const available = (sessionId ? reasoning : null)?.supported_levels
      ?? bootstrap?.reasoning?.supported_levels ?? ['provider_default']
    if (!available.includes(level as ReasoningLevel)) throw new Error(`当前模型不支持此推理档位：${level}`)
    modelChangeRef.current = true
    setModelChanging(true)
    try {
      const target = sessionId ?? await createSession()
      await lumenApi.updateSessionSettings(target, { reasoningEffort: level as ReasoningLevel })
      const snapshot = await lumenApi.session(target)
      if (selectedSessionRef.current === target) setReasoning(snapshot.reasoning ?? null)
      setSettingsNotice(`推理强度已设为 ${level}，用于此任务的下一轮对话。`)
    } finally {
      modelChangeRef.current = false
      setModelChanging(false)
    }
  }, [bootstrap, reasoning, sessionId, createSession])

  const handleSlashCommand = useCallback(
    async (command: string): Promise<boolean> => {
      const parts = command.trim().split(/\s+/)
      const name = parts[0].toLowerCase()
      const argument = parts.slice(1).join(' ')
      if (name === '/clear') {
        dispatch({ type: 'clear-visible' })
        setInput('')
        return true
      }
      if (name === '/new') {
        await createSession()
        setInput('')
        return true
      }
      if (name === '/retry') {
        if (!sessionId) {
          dispatch({ type: 'local-message', message: '还没有可重试的任务。' })
          setInput('')
          return true
        }
        const started = await lumenApi.retryRun(sessionId, requestId())
        dispatch({ type: 'run-registered', runId: started.runId })
        attachRun(started.runId)
        setInput('')
        return true
      }
      if (name === '/plan') {
        if (run.runId || bootstrap?.activeRunId) {
          dispatch({ type: 'local-error', message: '/plan 在任务运行期间不可用，请先停止当前任务。' })
          return true
        }
        const targetSession = sessionId ?? (await createSession())
        if (!argument) {
          const current = run.sessionSettings?.collaborationMode ?? bootstrap?.collaborationMode ?? 'default'
          const next = current === 'plan' ? 'default' : 'plan'
          await updateSessionSettings(targetSession, { collaborationMode: next })
          dispatch({ type: 'local-message', message: next === 'plan'
            ? '已进入 Plan 模式。输入任务开始规划，再次输入 /plan 返回直接执行。'
            : '已返回直接执行模式。' })
          setInput('')
          return true
        }
        await updateSessionSettings(targetSession, { collaborationMode: 'plan' })
        const started = await lumenApi.startRun(targetSession, argument, requestId())
        dispatch({ type: 'run-registered', runId: started.runId })
        attachRun(started.runId)
        await refreshChrome()
        setInput('')
        return true
      }
      if (name === '/mode') {
        if (!argument) {
          setModeMenuOpen(true)
          setInput('')
          return true
        }
        if (argument === 'plan') {
          await setCollaborationMode('plan')
        } else if (argument === 'auto' || argument === 'manual' || argument === 'accept_edits') {
          await setApprovalMode(argument, true)
        } else {
          dispatch({ type: 'local-error', message: `不支持的模式：${argument}` })
        }
        setInput('')
        return true
      }
      if (name === '/model') {
        if (!argument) {
          setModelSettingsView('models')
        } else {
          await selectModel(argument)
        }
        setInput('')
        return true
      }
      if (name === '/thinking') {
        if (!argument) setModelSettingsView('settings')
        else await selectThinking(argument)
        setInput('')
        return true
      }
      if (name === '/tasks') {
        setTranscriptOpen(true)
        setInput('')
        return true
      }
      if (name === '/help') {
        dispatch({
          type: 'local-message',
          message: HELP_TEXT,
        })
        setInput('')
        return true
      }
      if (name.startsWith('/skill:')) {
        const skillName = name.slice('/skill:'.length)
        if (!skillName) return true
        const targetSession = sessionId ?? (await createSession())
        const started = await lumenApi.invokeSkill(targetSession, skillName, argument, requestId())
        dispatch({ type: 'run-registered', runId: started.runId })
        attachRun(started.runId)
        await refreshChrome()
        setInput('')
        return true
      }
      if (name === '/prompt') {
        if (run.runId) {
          dispatch({ type: 'local-error', message: '/prompt 在任务运行期间不可用，请先停止当前任务。' })
          setInput('')
          return true
        }
        const invocation = parsePromptInvocation(command)
        const targetSession = sessionId ?? (await createSession())
        const started = await lumenApi.invokePrompt(
          targetSession,
          invocation.reference,
          invocation.arguments,
          command.trim(),
          requestId(),
        )
        dispatch({ type: 'run-registered', runId: started.runId })
        attachRun(started.runId)
        await refreshChrome()
        setInput('')
        return true
      }
      if (name === '/skill' && parts[1] === 'unload') {
        if (!sessionId || !parts[2]) {
          dispatch({ type: 'local-error', message: '用法：/skill unload <name>' })
        } else {
          await lumenApi.setContextSource(sessionId, 'skill', parts[2], false)
          dispatch({ type: 'local-message', message: `已从当前会话卸载 Skill：${parts[2]}` })
        }
        setInput('')
        return true
      }
      if (name === '/resource') {
        const unloading = parts[1] === 'unload'
        const refreshing = parts[1] === 'refresh'
        const reference = unloading || refreshing ? parts[2] : parts[1]
        if (!sessionId || !reference) {
          dispatch({
            type: 'local-error',
            message: '用法：/resource [refresh|unload] <reference>',
          })
        } else {
          await lumenApi.setContextSource(sessionId, 'resource', reference, !unloading)
          dispatch({
            type: 'local-message',
            message: `${unloading ? '已卸载' : refreshing ? '已刷新' : '已激活'} MCP resource：${reference}`,
          })
        }
        setInput('')
        return true
      }
      if (name === '/clarification' && parts[1] === 'cancel') {
        if (sessionId) {
          await lumenApi.cancelClarification(sessionId)
          dispatch({ type: 'local-message', message: '已取消待回答澄清。' })
        }
        setInput('')
        return true
      }
      if (name === '/context') {
        if (!sessionId) {
          dispatch({ type: 'local-message', message: '当前还没有会话上下文。' })
        } else if (parts[1] === 'sources') {
          const sources = await lumenApi.contextSources(sessionId)
          dispatch({
            type: 'local-message',
            message: sources.map((source) => (
              `${source.kind}  ${source.reference}  ${source.revision.slice(0, 16)}  ${source.status}`
            )).join('\n') || '当前会话没有激活的上下文来源。',
          })
        } else {
          const result = await lumenApi.context(sessionId)
          dispatch({ type: 'local-message', message: formatControlResult(result) })
        }
        setInput('')
        return true
      }
      if (name === '/instructions') {
        const result = await lumenApi.instructions()
        const sources = result.sources.map((source) => (
          `- ${source.role} · ${source.origin} · ${source.revision}`
        )).join('\n')
        dispatch({
          type: 'local-message',
          message: [
            `模式：${result.mode}  preset：${result.preset ?? '-'}  版本：${result.version}`,
            `稳定指令：${result.characters} 字符  动态上下文：${result.runtime_context_characters} 字符`,
            `模型：${result.active_model} (${result.model_id})`,
            '来源：',
            sources || '- 无',
          ].join('\n'),
        })
        setInput('')
        return true
      }
      if (name === '/compact') {
        if (sessionId) {
          const result = await lumenApi.compactContext(sessionId, argument)
          dispatch({ type: 'local-message', message: result.message })
        } else {
          dispatch({ type: 'local-message', message: '当前没有上下文。' })
        }
        setInput('')
        return true
      }
      if (name === '/memory') {
        if (!sessionId) {
          dispatch({ type: 'local-message', message: '请先创建会话再操作 memory。' })
        } else {
          const action = parts[1] ?? 'list'
          const rest = parts.slice(2).join(' ')
          const payload: Record<string, unknown> = {}
          if (action === 'remember') payload.content = rest
          if (action === 'forget' || action === 'edit') payload.target = rest
          if (action === 'use' || action === 'learn' || action === 'incognito') {
            payload.enabled = (parts[2] ?? 'on') !== 'off'
          }
          const result = await lumenApi.memoryControl(sessionId, action, payload)
          dispatch({ type: 'local-message', message: formatControlResult(result) })
        }
        setInput('')
        return true
      }
      if (name === '/tools' || name === '/skills' || name === '/mcp') {
        const rows = name === '/tools'
          ? bootstrap?.tools.map((item) => `${item.name} [${item.risk}] ${item.origin}`)
          : name === '/skills'
            ? bootstrap?.skills.map((item) => `${item.name} — ${item.description}`)
            : bootstrap?.mcp.map((item) => JSON.stringify(item))
        dispatch({ type: 'local-message', message: rows?.join('\n') || `No ${name.slice(1)} available.` })
        setInput('')
        return true
      }
      if (name === '/prompts') {
        const prompts = await lumenApi.mcpPrompts()
        dispatch({
          type: 'local-message',
          message: prompts.map((prompt) => (
            `${prompt.reference}  args=${prompt.arguments.join(',') || '-'}  ${prompt.description}`
          )).join('\n') || '没有可用的 MCP prompt。',
        })
        setInput('')
        return true
      }
      if (name === '/hooks') {
        const hooks = await lumenApi.hooks()
        dispatch({
          type: 'local-message',
          message: hooks.map((hook) => (
            `${hook.event}  ${hook.matcher}  ${hook.runner}  denied=${hook.deny_count}  last=${hook.last_triggered ?? '-'}`
          )).join('\n') || '没有配置 hooks。',
        })
        setInput('')
        return true
      }
      if (name === '/copy') {
        const latest = groupTimelineByTurn(run.timeline).flatMap((turn) => (
          turnPresentation(turn.response, turn.user?.text, Boolean(run.runId)).foreground
        )).findLast((item) => item.kind === 'assistant')
        if (!latest?.text) {
          dispatch({ type: 'local-message', message: '还没有可复制的助手回复。' })
        } else {
          await navigator.clipboard.writeText(latest.text)
          dispatch({ type: 'local-message', message: '已复制最新助手回复。' })
        }
        setInput('')
        return true
      }
      if (name === '/agents') {
        if (sessionId) await openInspector()
        else dispatch({ type: 'local-message', message: '当前还没有 Session。' })
        setInput('')
        return true
      }
      if (name === '/checkpoints') {
        if (sessionId) await openCheckpoints()
        else dispatch({ type: 'local-message', message: '当前还没有 Session。' })
        setInput('')
        return true
      }
      if (name === '/transcript') {
        setTranscriptOpen(true)
        setInput('')
        return true
      }
      if (name === '/dequeue') {
        setInput('')
        await dequeueInputs()
        return true
      }
      dispatch({ type: 'local-error', message: `未知命令：${name}。输入 / 查看可用命令。` })
      setInput('')
      return true
  },
    [
      attachRun,
      bootstrap,
      createSession,
      dequeueInputs,
      openCheckpoints,
      openInspector,
      refreshChrome,
      run.runId,
      run.sessionSettings,
      run.timeline,
      sessionId,
      selectModel,
      selectThinking,
      setApprovalMode,
      setCollaborationMode,
      updateSessionSettings,
    ],
  )

  const executeCommand = useCallback(async (command: string, preserveDraft = false) => {
    const draft = input
    try {
      await handleSlashCommand(command)
      if (preserveDraft && ['/model', '/thinking', '/mode', '/tasks', '/copy', '/checkpoints', '/skills', '/tools'].includes(command)) {
        setInput(draft)
      }
    } catch (error) {
      if (error instanceof AuthenticationError) return
      dispatch({ type: 'local-error', message: error instanceof Error ? error.message : '命令执行失败' })
    }
  }, [handleSlashCommand, input])

  const submit = useCallback(async () => {
    if (submitRequestRef.current || modelChangeRef.current || approvalChangeRef.current || editingRef.current || stoppingRef.current || planReviewRequest.current) return
    const prompt = input.trim() || (attachments.length ? '请分析这些图片。' : '')
    if (!prompt) return
    submitRequestRef.current = true
    let targetSession = sessionId
    try {
      if (prompt.startsWith('/') && (await handleSlashCommand(prompt))) return
      if (attachments.length && !bootstrap?.inputModalities.includes('image')) {
        dispatch({ type: 'local-error', message: '当前模型不支持图片输入，请先切换到视觉模型。' })
        return
      }
      if (run.runId) {
        await lumenApi.queueInput(run.runId, prompt, queueMode, attachments)
        if (selectedSessionRef.current !== targetSession) return
        setInput(current => current === input ? '' : current)
        setAttachments(current => current === attachments ? [] : current)
        return
      }
      targetSession = sessionId ?? (await createSession())
      const started = await lumenApi.startRun(targetSession, prompt, requestId(), attachments, undefined, assistantContext)
      if (selectedSessionRef.current !== targetSession) { void refreshChrome(); return }
      if (['completed', 'failed', 'cancelled'].includes(started.status)) {
        await openSession(targetSession)
        return
      }
      dispatch({ type: 'run-registered', runId: started.runId })
      attachRun(started.runId)
      await refreshChrome()
      if (selectedSessionRef.current === targetSession) {
        setInput(current => current === input ? '' : current)
        setAttachments(current => current === attachments ? [] : current)
      }
    } catch (error) {
      if (error instanceof AuthenticationError) return
      if (selectedSessionRef.current === targetSession) dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : '无法启动运行',
      })
    } finally { submitRequestRef.current = false }
  }, [
    attachRun,
    assistantContext,
    openSession,
    attachments,
    bootstrap?.inputModalities,
    createSession,
    handleSlashCommand,
    input,
    queueMode,
    refreshChrome,
    run.runId,
    sessionId,
  ])

  const answerClarification = useCallback(async (answer: string) => {
    if (!sessionId || run.runId || !run.pendingClarification || modelChangeRef.current
      || approvalChangeRef.current || editingRef.current || planReviewRequest.current) {
      throw new Error('当前无法提交回答，请等待运行结束。')
    }
    const started = await lumenApi.startRun(sessionId, answer, requestId(), [])
    dispatch({ type: 'run-registered', runId: started.runId })
    attachRun(started.runId)
    void refreshChrome()
  }, [sessionId, run.runId, run.pendingClarification, attachRun, refreshChrome])

  const stop = useCallback(async () => {
    if (!run.runId || stoppingRef.current) return
    const runId = run.runId
    stoppingRef.current = runId
    setStopping(true)
    setStopError('')
    try {
      await lumenApi.cancelRun(runId)
    } catch (error) {
      if (stoppingRef.current === runId) setStopError(error instanceof Error ? error.message : '停止失败，请重试。')
    } finally {
      if (stoppingRef.current === runId) {
        stoppingRef.current = null
        setStopping(false)
      }
    }
  }, [run.runId])

  useEffect(() => {
    stoppingRef.current = null
    setStopping(false)
    setStopError('')
  }, [run.runId, sessionId])

  const editMessage = useCallback(async (item: TimelineEntry, text: string) => {
    if (!sessionId || editingRef.current || run.runId || bootstrap?.activeRunId || modelChangeRef.current || approvalChangeRef.current) {
      throw new Error('请等待当前运行结束后再编辑消息。')
    }
    const sourceId = sessionId
    editingRef.current = true
    setMessageEditing(true)
    try {
      // Resolve the durable turn identity, including a just-finished SSE row.
      const source = await lumenApi.session(sourceId)
      const recorded = source.timeline.find((entry) => entry.kind === 'user' && (
        entry.id === item.id || (item.interactionId && entry.interaction_id === item.interactionId)
      ))
      if (!recorded || typeof recorded.turn_index !== 'number') throw new Error('消息尚未保存，请刷新后重试。')
      const skill = /^\/skill:([^\s]+)(?:\s+([\s\S]*))?$/.exec(text)
      const prompt = /^\/prompt(?:\s|$)/.test(text) ? parsePromptInvocation(text) : null
      const original = runReducer(initialRunState, { type: 'snapshot', snapshot: source }).timeline
        .find((entry) => entry.id === recorded.id)
      if ((skill || prompt) && original?.attachments?.length) {
        throw new Error('Skill 和 Prompt 命令暂不支持携带图片，请使用普通消息重新生成，以保留附件。')
      }
      const key = JSON.stringify([sourceId, recorded.turn_index, text])
      if (editRequestRef.current?.key !== key) editRequestRef.current = {
        key, runRequestId: requestId(),
      }
      const intent = editRequestRef.current
      if (selectedSessionRef.current !== sourceId) throw new Error('已切换任务，未在此处启动重新生成。')
      const started = skill
        ? await lumenApi.invokeSkill(
          sourceId, skill[1], skill[2] ?? '', intent.runRequestId, recorded.turn_index,
        )
        : prompt
          ? await lumenApi.invokePrompt(
            sourceId, prompt.reference, prompt.arguments, text, intent.runRequestId,
            recorded.turn_index,
          )
          : await lumenApi.startRun(
            sourceId, text, intent.runRequestId, original?.attachments ?? [], recorded.turn_index,
          )
      if (selectedSessionRef.current === sourceId) {
        const snapshot = await lumenApi.session(sourceId)
        if (selectedSessionRef.current !== sourceId) return
        closeStreamRef.current?.()
        applySessionSnapshot(snapshot)
        dispatch({ type: 'run-registered', runId: started.runId })
        stickToLatestRef.current = true
        attachRun(started.runId)
        setSettingsNotice('已在当前对话中从该消息重新生成；后续旧回答不会进入模型上下文。')
      }
      editRequestRef.current = null
      void refreshChrome()
    } finally {
      editingRef.current = false
      setMessageEditing(false)
    }
  }, [sessionId, run.runId, bootstrap?.activeRunId, applySessionSnapshot, attachRun, refreshChrome])

  const approveAuto = useCallback(async () => {
    if (!confirmAuto || confirmAuto !== sessionId || approvalChangeRef.current) return
    approvalChangeRef.current = true
    setApprovalChanging(true)
    setAutoError('')
    try {
      await updateSessionSettings(confirmAuto, confirmPreset ? {
        permissionPreset: confirmPreset,
        ...(confirmPreset === 'full_access' ? { confirmFullAccess: true } : {}),
      } : { approvalMode: 'auto' })
      setConfirmAuto((current) => current === confirmAuto ? null : current)
    } catch (error) {
      setAutoError(error instanceof Error ? error.message : '切换失败，请重试。')
    } finally {
      approvalChangeRef.current = false
      setApprovalChanging(false)
    }
  }, [confirmAuto, confirmPreset, sessionId, updateSessionSettings])

  const reviewPlan = useCallback(async (action: 'approve' | 'reject') => {
    if (!sessionId || selectedSessionRef.current !== sessionId || !run.planReviewRevision
      || planReviewRequest.current || run.runId || bootstrap?.activeRunId
      || !['review_pending', 'approved_waiting_to_execute'].includes(run.planReviewStatus ?? '')) return
    const feedback = planFeedback.trim()
    if (action === 'reject' && !feedback) {
      dispatch({ type: 'local-error', message: '驳回计划时需要填写反馈。' })
      return
    }
    const operation = Symbol('plan-review')
    planReviewRequest.current = operation
    setPlanReviewBusy(true)
    try {
      const started = await lumenApi.reviewPlan(
        sessionId,
        action,
        run.planReviewRevision,
        requestId(),
        feedback,
      )
      if (selectedSessionRef.current !== sessionId || planReviewRequest.current !== operation) return
      dispatch({ type: 'run-registered', runId: started.runId })
      dispatch({ type: 'session-settings', sessionId, settings: {
        collaborationMode: action === 'approve' ? 'default' : 'plan',
      } })
      setPlanFeedback('')
      attachRun(started.runId)
    } catch (error) {
      if (error instanceof AuthenticationError) return
      if (selectedSessionRef.current !== sessionId || planReviewRequest.current !== operation) return
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : '计划审批失败',
      })
    } finally {
      if (planReviewRequest.current === operation) {
        planReviewRequest.current = null
        setPlanReviewBusy(false)
      }
    }
  }, [attachRun, bootstrap?.activeRunId, planFeedback, run.planReviewRevision, run.planReviewStatus, run.runId, sessionId])

  const leaveSession = useCallback(() => {
    setAssistantPage('chat')
    setAssistantContext([]); setContextResource(null); setAssistantNotice('')
    setContextTab('discussion')
    planReviewRequest.current = null
    setPlanReviewBusy(false)
    setPlanFeedback('')
    setSidebarOpen(false)
    setConfirmAuto(null)
    setAutoError('')
    setSettingsNotice('')
    closeStreamRef.current?.()
    closeStreamRef.current = null
    setSessionId(null)
    setReasoning(null)
    setModelSettingsView(null)
    selectedSessionRef.current = null
    setSessionMenu(null)
    dispatch({ type: 'reset' })
    const url = new URL(window.location.href)
    url.searchParams.delete('session')
    url.searchParams.delete('view'); url.searchParams.delete('id')
    window.history.replaceState({}, '', `${url.pathname}${url.search}`)
  }, [])

  const discussAssistant = async ({intent, context}: AssistantDiscussion) => {
    try {
      if (context) {
        const discussion = await lumenApi.assistantDiscussion(context)
        await openSession(discussion.session_id)
        setAssistantContext(discussion.context_refs)
        if (context.kind === 'resource') setContextResource(await lumenApi.assistantResource(context.id, false, context.revision ?? undefined))
        const draft = discussionDrafts.current.get(discussion.session_id) ?? ''
        setInput(draft)
        if (intent) setInput(draft ? `${draft}\n\n${intent}` : intent)
        else if (!draft && context.kind === 'goal') setInput('请根据这个目标，与我一起确认现状、成功标准和下一步。')
      } else {
        leaveSession()
        if (intent) setInput(current => current ? `${current}\n\n${intent}` : intent)
      }
    } catch (error) {
      setAssistantNotice(error instanceof Error ? error.message : '无法继续讨论')
    }
  }
  const planningAttempts = useRef(new Set<string>())
  const planGoal = async (goalId: string, identity: string) => {
    if (submitRequestRef.current || modelChangeRef.current || approvalChangeRef.current || editingRef.current
      || stoppingRef.current || planReviewRequest.current || (!planningAttempts.current.has(identity) && (bootstrap?.activeRunId || run.runId))) {
      throw new Error('工作区正在处理其他操作，请结束后重试规划。')
    }
    submitRequestRef.current = true
    const originView = window.location.href
    try {
      const discussion = await lumenApi.assistantDiscussion({kind: 'goal', id: goalId})
      planningAttempts.current.add(identity)
      const started = await lumenApi.startRun(discussion.session_id,
        '请围绕关联目标，与我一起确认现状、可验证的成功标准和第一步。已有说明与原对话约束继续有效。先提出规划；不要自行启用持续推进或定时跟进。',
        `goal-plan:${identity}`, [], undefined, discussion.context_refs)
      if (window.location.href !== originView) { void refreshChrome(); return }
      const draft = discussionDrafts.current.get(discussion.session_id) ?? ''
      await openSession(discussion.session_id)
      if (selectedSessionRef.current === discussion.session_id) {
        setAssistantContext(discussion.context_refs)
        setInput(draft)
        if (!['completed', 'failed', 'cancelled'].includes(started.status)) {
          dispatch({type: 'run-registered', runId: started.runId})
          attachRun(started.runId)
        }
      }
      await refreshChrome()
    } finally { submitRequestRef.current = false }
  }
  const ideaAttempts = useRef(new Map<string, {sessionId: string; requestId: string; refs: AssistantContextRef[]}>())
  const startIdea = async (idea: AssistantIdea, prompt: string) => {
    if (submitRequestRef.current || modelChangeRef.current || approvalChangeRef.current || editingRef.current
      || stoppingRef.current || planReviewRequest.current || bootstrap?.activeRunId) {
      throw new Error('工作区正在处理其他操作，请结束后再开始这项任务。')
    }
    submitRequestRef.current = true
    const originView = window.location.href
    try {
      const current = (await lumenApi.assistant()).ideas.find(item => item.id === idea.id && !item.dismissed)
      if (!current || current.intent !== idea.intent || JSON.stringify(current.context_refs) !== JSON.stringify(idea.context_refs))
        throw new Error('这条建议已更新或撤回，请刷新灵感后重新查看方案。')
      const key = JSON.stringify([idea.id, prompt])
      let attempt = ideaAttempts.current.get(key)
      if (!attempt) {
        const discussion = current.context_refs?.length
          ? await lumenApi.assistantDiscussion(current.context_refs[0]) : null
        const sessionId = discussion?.session_id ?? (await lumenApi.createSession()).sessionId
        attempt = {sessionId, requestId: requestId(), refs: discussion?.context_refs ?? []}
        ideaAttempts.current.set(key, attempt)
      }
      const started = await lumenApi.startRun(attempt.sessionId, prompt, attempt.requestId, [], undefined, attempt.refs)
      if (window.location.href !== originView) { void refreshChrome(); return }
      // Navigation happens after admission: a failed start keeps the proposal and retry identity.
      const draft = discussionDrafts.current.get(attempt.sessionId) ?? ''
      await openSession(attempt.sessionId)
      if (selectedSessionRef.current === attempt.sessionId) {
        setInput(draft)
        if (!['completed', 'failed', 'cancelled'].includes(started.status)) {
          dispatch({type: 'run-registered', runId: started.runId})
          attachRun(started.runId)
        }
      }
      await refreshChrome()
    } finally { submitRequestRef.current = false }
  }
  const publishableInteractionId = groupTimelineByTurn(run.timeline)
    .findLast(turn => turn.response.some(item => item.kind === 'assistant'))?.user?.interactionId
  const goalId = assistantContext.find(ref => ref.kind === 'goal')?.id
  useEffect(() => {
    let disposed = false
    setContextGoal(null)
    if (!goalId || !lumenApi.assistantGoal) return
    const refresh = async () => {
      try {
        const detail = await lumenApi.assistantGoal(goalId)
        if (disposed) return
        setContextGoal(detail.goal)
        const execution = detail.goal.execution
        if (assistantPage === 'chat' && execution?.status === 'active' && execution.run_id && execution.run_id !== run.runId
          && detail.goal.primary_session_id && detail.goal.primary_session_id === selectedSessionRef.current) {
          const snapshot = await lumenApi.session(detail.goal.primary_session_id)
          if (!disposed && snapshot.sessionId === selectedSessionRef.current && snapshot.activeRunId === execution.run_id) {
            applySessionSnapshot(snapshot)
            attachRun(execution.run_id)
          }
        }
      } catch { /* Re-read after the next status update without changing the conversation. */ }
    }
    void refresh()
    const timer = setInterval(() => {if (!document.hidden) void refresh()}, 1500)
    return () => { disposed = true; clearInterval(timer) }
  }, [goalId, run.runId, run.status, assistantPage, applySessionSnapshot, attachRun])
  useEffect(() => setSessionFiles([]), [sessionId])
  useEffect(() => {
    let disposed = false
    if (sessionId && lumenApi.assistantResources) {
      void lumenApi.assistantResources('', 'all', undefined, sessionId).then(value => {
        if (!disposed) setSessionFiles(value.items.filter(resource => resource.file_path && resource.session_id === sessionId))
      }).catch(() => { /* Existing file links remain available if the index cannot be read. */ })
    }
    return () => { disposed = true }
  }, [run.runId, sessionId])
  const applyResourceAnswer = async () => {
    if (!sessionId || !publishableInteractionId || publishing || !contextResource || contextResource.resource.file_path) return
    setPublishing(true); setAssistantNotice('')
    try {
      const result = await lumenApi.publishAnswer({session_id: sessionId, interaction_id: publishableInteractionId,
        title: contextResource.resource.title,
        target_resource_id: contextResource.resource.id,
        expected_revision: contextResource.resource.revision,
      })
      if (selectedSessionRef.current !== sessionId) return
      const next = await lumenApi.assistantResource(result.resource.id)
      if (selectedSessionRef.current !== sessionId) return
      setContextResource(next)
      setAssistantContext([{kind: 'resource', id: next.resource.id, revision: next.resource.revision}])
      setAssistantNotice('修改已应用，原版本仍可查看。')
    } catch (error) {
      if (selectedSessionRef.current === sessionId) setAssistantNotice(error instanceof Error ? error.message : '保存失败，可保留回答后重试')
    } finally { setPublishing(false) }
  }

  const decideApproval = async (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => {
    if (!run.runId || approvalRequestRef.current) return
    approvalRequestRef.current = true
    setApprovalSubmitting(true); setApprovalError('')
    try {
      const result = await lumenApi.decideApproval(run.runId, callId, approved, scope)
      if (selectedSessionRef.current !== sessionId) return
      dispatch({type: 'approval-decided', callIds: result.resolved_call_ids ?? [callId], approved})
    } catch (error) {
      if (selectedSessionRef.current === sessionId) setApprovalError(error instanceof Error ? error.message : '授权未送达，请重试。')
    } finally { approvalRequestRef.current = false; setApprovalSubmitting(false) }
  }
  useEffect(() => { setApprovalError('') }, [sessionId, run.runId])

  const setArchived = useCallback(async (session: SessionSummary, archived: boolean) => {
    setSessionManagementBusy(true)
    try {
      if (archived) await lumenApi.archiveSession(session.sessionId)
      else {
        await lumenApi.restoreSession(session.sessionId)
        setSessionView('active')
      }
      if (archived && session.sessionId === sessionId) leaveSession()
      setSessionMenu(null)
      await refreshChrome()
    } catch (error) {
      if (error instanceof AuthenticationError) return
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : archived ? '归档失败' : '恢复失败',
      })
    } finally {
      setSessionManagementBusy(false)
    }
  }, [leaveSession, refreshChrome, sessionId])

  const commitSessionDialog = useCallback(async (title?: string) => {
    if (!sessionDialog) return
    setSessionManagementBusy(true)
    try {
      if (sessionDialog.mode === 'rename') {
        await lumenApi.renameSession(sessionDialog.session.sessionId, title?.trim() ?? '')
      } else {
        await lumenApi.deleteSession(sessionDialog.session.sessionId)
        if (sessionDialog.session.sessionId === sessionId) leaveSession()
      }
      setSessionDialog(null)
      setSessionMenu(null)
      await refreshChrome()
    } catch (error) {
      if (error instanceof AuthenticationError) return
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : '任务管理操作失败',
      })
    } finally {
      setSessionManagementBusy(false)
    }
  }, [leaveSession, refreshChrome, sessionDialog, sessionId])

  const scrollToLatest = useCallback((behavior: ScrollBehavior = 'smooth') => {
    const timeline = timelineRef.current
    if (!timeline) return
    stickToLatestRef.current = true
    setShowJumpToLatest(false)
    timeline.scrollTo({ top: timeline.scrollHeight, behavior })
  }, [])

  const trackTimelineScroll = useCallback(() => {
    const timeline = timelineRef.current
    if (!timeline) return
    const atLatest = timeline.scrollHeight - timeline.scrollTop - timeline.clientHeight < 72
    stickToLatestRef.current = atLatest
    setShowJumpToLatest(!atLatest)
  }, [])

  useEffect(() => {
    if (!stickToLatestRef.current) return
    const frame = window.requestAnimationFrame(() => scrollToLatest('auto'))
    return () => window.cancelAnimationFrame(frame)
  }, [run.timeline, run.runId, run.queuedInputs.length, scrollToLatest])

  const activeSession = sessions.find((item) => item.sessionId === sessionId)
  useEffect(() => {
    document.title = `${activeSession?.title ?? '新对话'} · Lumen`
  }, [activeSession?.title])
  const scrollNavItems = useMemo(() => run.timeline.filter((item) => item.kind === 'user')
    .map(({ id, text }) => ({ id, text })), [run.timeline])
  const sessionIsArchived = Boolean(activeSession?.archived)
  const visibleSessions = sessions.filter((item) => item.archived === (sessionView === 'archived'))
  const workspaceBusy = Boolean(bootstrap?.activeRunId || run.runId || messageEditing || planReviewBusy)
  const pendingApprovals = run.runId ? run.timeline.filter(item => item.pendingApproval) : []
  const approvalMode = run.sessionSettings?.approvalMode ?? bootstrap?.approvalMode ?? 'manual'
  const sandboxMode = run.sessionSettings?.sandboxMode ?? bootstrap?.sandboxMode ?? 'workspace_write'
  const filesystemScope = run.sessionSettings?.filesystemScope ?? bootstrap?.filesystemScope
    ?? (sandboxMode === 'disabled' ? 'full' : 'workspace')
  const permissionPreset = filesystemScope === 'configured' ? 'custom' : approvalMode === 'auto'
    ? (sandboxMode === 'disabled' ? 'full_access' : 'workspace_auto')
    : sandboxMode === 'disabled' ? 'custom' : approvalMode
  const accessScope = filesystemScope === 'configured' ? '配置目录' : sandboxMode === 'disabled' ? '本机文件' : '仅工作区'
  const collaborationMode = run.sessionSettings?.collaborationMode ?? bootstrap?.collaborationMode ?? 'default'
  const awaitingPlanReview = (run.planReviewStatus === 'review_pending' || run.planReviewStatus === 'approved_waiting_to_execute')
    && Boolean(run.planReviewRevision)
  // A Session may retain an old plan while a new, unrelated turn starts.
  const hasCurrentPlan = Boolean(run.plan?.steps.length) && run.timeline.findLastIndex((item) => item.kind === 'plan')
    > run.timeline.findLastIndex((item) => item.kind === 'user')
  const isLanding = !sessionIsArchived
    && !loading
    && run.timeline.length === 0
    && !run.runId
    && run.queuedInputs.length === 0
  const statusLabel = authenticationRequired ? '等待重新连接' : connectionError ? '连接中断'
    : planReviewBusy ? '正在提交方案' : messageEditing ? '正在重新生成'
    : run.runId
    ? pendingApprovals.length ? '等待授权' : '运行中'
    : workspaceBusy
      ? '其他会话运行中'
      : run.status === 'failed'
        ? (run.pendingEffects.length ? '等待核实操作结果' : '运行失败')
        : run.status === 'cancelled'
          ? '已停止'
          : run.status === 'completed'
            ? '已完成'
            : run.status === 'waiting_for_user'
              ? '等待回答'
            : '就绪'
  const slashCommands = useMemo(() => buildSlashCommands(bootstrap), [bootstrap])
  const thinkingSelection = (sessionId ? reasoning : null) ?? bootstrap?.reasoning
  const thinkingAvailable = (thinkingSelection?.supported_levels.length ?? 0) > 1
  const thinkingUnavailable = thinkingSelection?.capability_status === 'unsupported'
    ? '不支持调节' : '推理控制未配置'
  const activeModelName = modelDisplayName(bootstrap?.modelId, bootstrap?.activeModel ?? '')
  const pendingModelName = modelDisplayName(bootstrap?.modelIds?.[bootstrap?.pendingModel ?? ''], bootstrap?.pendingModel ?? '')
  const composerSettings = (
    <div className="composer-settings">
      <ModelSettingsControl
        view={modelSettingsView}
        onViewChange={setModelSettingsView}
        model={bootstrap?.activeModel ?? ''}
        modelId={bootstrap?.modelId}
        pendingModel={bootstrap?.pendingModel}
        models={bootstrap?.availableModels ?? []}
        modelIds={bootstrap?.modelIds}
        disabled={!bootstrap || loading || messageEditing || planReviewBusy || modelChanging}
        modelNotice={bootstrap?.pendingModel ? `本轮继续使用 ${activeModelName}；结束后切换到 ${pendingModelName}。` : '用于下一轮对话；运行中切换模型会在本轮结束后生效。'}
        onModelChange={selectModel}
        defaultLevel={thinkingSelection?.provider_default_level}
        value={thinkingSelection?.requested
          ?? (thinkingSelection?.mapping === 'unverified' ? 'configured' : 'provider_default')}
        reasoningDisabled={Boolean(bootstrap?.pendingModel) || !thinkingAvailable}
        description={thinkingSelection?.mapping === 'unverified'
          ? '当前沿用原始配置，无法确认有效档位。选择档位将使用已校验的设置。'
          : `${activeModelName} · 用于下一轮对话。跟随供应商默认时不发送推理参数；`
            + (thinkingSelection?.provider_default_level
              ? `官方文档默认 ${thinkingSelection.provider_default_level}。`
              : '此路由的默认档位尚未确认。')}
        options={[
          ...(thinkingSelection?.mapping === 'unverified'
            ? [{ value: 'configured', label: '原始配置（未校验）' }] : []),
          ...reasoningOptions(thinkingSelection).map((option) => option.value === 'provider_default'
            && !thinkingAvailable ? { ...option, label: thinkingUnavailable, compactLabel: thinkingUnavailable }
              : option),
        ]}
        onChange={selectThinking}
      />
      {collaborationMode === 'plan' && <span className="composer-plan-indicator" role="status">
        <ListChecks size={14} aria-hidden="true" /> Plan
      </span>}
      <div className="composer-permissions">
      <span className="composer-permission-scope">{accessScope}</span>
      <ChoiceMenu
        label="权限"
        open={modeMenuOpen}
        onOpenChange={setModeMenuOpen}
        value={permissionPreset}
        disabled={!bootstrap || loading || workspaceBusy || approvalChanging}
        description={`当前访问范围：${accessScope}。仅影响当前对话；切换访问范围前请结束运行。内建提交、推送仍每次确认。`}
        icon={<ShieldCheck size={14} />}
        options={permissionPreset === 'custom' ? [...permissionChoices, {
          value: 'custom', label: '自定义权限', description: `${accessScope}访问；审批方式：${approvalMode === 'manual' ? '修改与执行按规则确认' : approvalMode === 'auto' ? '常规操作自动执行' : '自动改文件'}。`,
        }] : permissionChoices}
        onChange={setPermissionPreset}
      />
      </div>
    </div>
  )

  return (
    <DocumentProvider workspace={bootstrap?.workspace ?? ''}><main className={`workspace-shell ${sidebarCollapsed ? 'is-sidebar-collapsed' : ''}`}>
      <nav className="sidebar-rail" ref={sidebarRailRef} aria-label="收起的侧边栏" inert={sessionSearchOpen}>
        <button type="button" aria-label="展开侧边栏" title="展开侧边栏" aria-expanded="false" aria-controls="session-sidebar" onClick={() => expandSidebar()}><SidebarSimple size={21} /></button>
        <button type="button" aria-label="新建任务" title="新建任务" onClick={leaveSession}><NotePencil size={21} /></button>
        <button type="button" aria-label="搜索任务" title="搜索任务" aria-haspopup="dialog" onClick={openSessionSearch}><MagnifyingGlass size={21} /></button>
      </nav>
      <aside id="session-sidebar" ref={sidebarFocusRef} className={`session-sidebar ${sidebarOpen ? 'is-open' : ''}`}
        role={sidebarOpen ? 'dialog' : undefined} aria-modal={sidebarOpen || undefined} aria-label="任务侧边栏" tabIndex={-1} inert={sessionSearchOpen}>
        <div className="sidebar-brand-row">
          <button className="brand" type="button" aria-label="返回新任务" onClick={leaveSession}>
            <LumenLogo />
          </button>
          <div className="sidebar-header-actions">
            <button className="sidebar-search-toggle" type="button" aria-label="搜索任务" title="搜索任务" aria-haspopup="dialog" onClick={openSessionSearch}><MagnifyingGlass size={19} /></button>
            <button className="sidebar-close" type="button" aria-label="收起侧边栏" title="收起侧边栏" aria-expanded="true" aria-controls="session-sidebar" onClick={collapseSidebar}>
              <SidebarSimple size={20} aria-hidden="true" />
            </button>
          </div>
        </div>
        <button className="new-session" type="button" onClick={leaveSession}>
          <NotePencil size={18} aria-hidden="true" /> 新建任务
        </button>
        <nav className="assistant-navigation" aria-label="个人助理">
          {assistantPages.filter(item => item.id !== 'automations').map(item => <button key={item.id} type="button"
            className={assistantPage === item.id ? 'is-active' : ''}
            aria-current={assistantPage === item.id ? 'page' : undefined}
            onClick={() => navigateAssistant(item.id)}>
            <item.icon size={18} aria-hidden="true" />{item.label}
          </button>)}
        </nav>
        <nav className="session-list" aria-label="历史任务">
          <div className="session-view-tabs" role="tablist" aria-label="任务视图">
            <button
              type="button"
              role="tab"
              aria-selected={sessionView === 'active'}
              className={sessionView === 'active' ? 'is-active' : ''}
              onClick={() => {
                setSessionView('active')
                setSessionMenu(null)
              }}
            >
              最近
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={sessionView === 'archived'}
              className={sessionView === 'archived' ? 'is-active' : ''}
              onClick={() => {
                setSessionView('archived')
                setSessionMenu(null)
              }}
            >
              已归档
            </button>
          </div>
          {visibleSessions.length === 0 ? (
            <div className="sidebar-empty">
              {sessionView === 'active' ? '还没有历史任务' : '还没有归档任务'}
            </div>
          ) : (
            visibleSessions.map((session) => (
              <div className="session-row-wrap" key={session.sessionId}>
                <div className={`session-row ${session.sessionId === sessionId ? 'is-active' : ''}`}>
                  <button
                    type="button"
                    className="session-open"
                    onClick={() => void openSession(session.sessionId)}
                  >
                    <span>{session.title}</span>
                  </button>
                  <button
                    type="button"
                    className="session-more"
                    aria-label={`管理任务：${session.title}`}
                    aria-haspopup="menu"
                    aria-expanded={sessionMenu?.session.sessionId === session.sessionId}
                    onClick={(event) => {
                      const anchor = event.currentTarget
                      setSessionMenu((current) => (
                        current?.session.sessionId === session.sessionId ? null : { session, anchor }
                      ))
                    }}
                  >
                    <DotsThree size={18} weight="bold" aria-hidden="true" />
                  </button>
                </div>
              </div>
            ))
          )}
        </nav>
        <div className="sidebar-workspace" title={bootstrap?.workspace}>
          <FolderSimple size={16} aria-hidden="true" />
          <span>{bootstrap?.workspace.split('/').at(-1) ?? 'workspace'}</span>
        </div>
        {sessionMenu && (
          <SessionActionsMenu
            anchor={sessionMenu.anchor}
            session={sessionMenu.session}
            busy={sessionManagementBusy}
            blocked={sessionMenu.session.sessionId === sessionId && workspaceBusy}
            onClose={() => setSessionMenu(null)}
            onRename={() => {
              setSessionDialog({ mode: 'rename', session: sessionMenu.session })
              setSessionMenu(null)
            }}
            onArchive={() => {
              const session = sessionMenu.session
              setSessionMenu(null)
              void setArchived(session, !session.archived)
            }}
            onDelete={() => {
              setSessionDialog({ mode: 'delete', session: sessionMenu.session })
              setSessionMenu(null)
            }}
          />
        )}
      </aside>

      {/* scrim 是鼠标点按的便捷关闭区,移出 Tab 顺序避免焦点停在全屏不可见元素上;键盘用户走 sidebar-close */}
      {sidebarOpen && (
        <button className="sidebar-scrim" type="button" tabIndex={-1} aria-label="关闭任务列表" onClick={() => setSidebarOpen(false)} />
      )}

      <section className="agent-workspace" inert={sidebarOpen || sessionSearchOpen}>
        <header className={`workspace-header ${isLanding ? 'is-landing' : ''}`}>
          <div className="workspace-heading">
            <button className="sidebar-toggle" ref={sidebarToggleRef} type="button" aria-label="展开侧边栏" aria-expanded={sidebarOpen} aria-controls="session-sidebar" onClick={() => expandSidebar()}>
              <SidebarSimple size={21} aria-hidden="true" />
            </button>
            <div>
              {assistantPage === 'chat' ? <h1>{activeSession?.title ?? '新对话'}</h1>
                : <span>{assistantPages.find(item => item.id === assistantPage)?.label}</span>}
              {(authenticationRequired || connectionError || (assistantPage === 'chat' && (workspaceBusy || run.status === 'failed' || run.status === 'cancelled'))) && <p>
                <span className={authenticationRequired || connectionError ? 'status-dot is-disconnected' : workspaceBusy ? 'status-dot is-busy' : 'status-dot'} />
                {statusLabel}{!authenticationRequired && !connectionError && usageLabel(run.usage)}
              </p>}
            </div>
          </div>
          <div className="workspace-controls" aria-label="工作区工具">
            <button type="button" className="assistant-presence" aria-label="活动与持续跟进" title="查看工作状态与持续跟进" onClick={() => navigateAssistant('automations')}>
              <LumenMark className="assistant-presence-mark" />
              <span><strong>Lumen</strong><small><i className={`status-dot ${workspaceBusy ? 'is-busy' : ''}`} />{authenticationRequired || connectionError ? '等待连接' : workspaceBusy ? '正在为你工作' : '随时可以开始'}</small></span>
            </button>
            {assistantPage === 'chat' && <button type="button" className="header-action"
              aria-label="将意图设为目标" title="设为目标" disabled={!sessionId && !input.trim()} onClick={() => {
                const latest = run.timeline.findLast(item => item.kind === 'user')
                setAutomationIntent(input.trim() || (latest?.kind === 'user' ? latest.text : ''))
                setGoalSourceSessionId(sessionId ?? undefined)
                setAssistantObjectId(undefined)
                setAssistantPage('goals')
              }}><ClockCounterClockwise size={17} aria-hidden="true" /></button>}
            {assistantPage === 'chat' && sessionId && <details className="workspace-tools-menu"><summary aria-label="更多工作区工具"><DotsThree size={20} /></summary><div>
              <button
                type="button"
                className="header-action"
                aria-label="打开智能体与工作状态"
                data-tooltip="运行时"
                disabled={!sessionId}
                onClick={() => void openInspector()}
              >
                <TreeStructure size={16} aria-hidden="true" />
                {run.agents.length > 0 && <span className="header-action-count">{run.agents.length}</span>}
              </button>
              <button
                type="button"
                className="header-action"
                aria-label="打开检查点"
                data-tooltip="检查点"
                disabled={!sessionId || workspaceBusy}
                onClick={() => void openCheckpoints()}
              >
                <ClockCounterClockwise size={16} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="header-action"
                aria-label="打开运行记录"
                data-tooltip="运行记录"
                disabled={!sessionId}
                onClick={() => setTranscriptOpen(true)}
              >
                <ListMagnifyingGlass size={16} aria-hidden="true" />
              </button>
              <span className="workspace-controls-divider" aria-hidden="true" />
            </div></details>}
            <button
              type="button"
              className="header-action"
              aria-label="打开系统设置"
              data-tooltip="设置"
              onClick={() => setSettingsOpen(true)}
            >
              <GearSix size={16} aria-hidden="true" />
            </button>
          </div>
        </header>

        {!workspaceBusy && run.pendingEffects.length > 0 && <div className="settings-notice" role="status">
          已保留产物，有 {run.pendingEffects.length} 项操作结果需要核实。处理后可继续原任务；编辑消息不会清除这些记录。
          <button type="button" onClick={() => void openInspector()}>查看并处理</button>
        </div>}

        {settingsOpen && (
          <SettingsDialog
            workspace={bootstrap?.workspace ?? ''}
            runActive={workspaceBusy}
            onClose={() => setSettingsOpen(false)}
            onModelConfigurationApplied={() => {
              setReasoning(null)
              void refreshChrome()
            }}
          />
        )}

        {inspectorOpen && (
          <RuntimeInspector
            agents={run.agents}
            workProducts={run.workProducts}
            pendingEffects={run.pendingEffects}
            recoverableEffects={run.recoverableEffects}
            busy={controlBusy || workspaceBusy}
            onClose={() => setInspectorOpen(false)}
            onRefresh={() => void refreshSessionCapabilities()}
            onAgentAction={(agent, action) => void runAgentAction(agent, action)}
            onWaive={waiveEffect}
          />
        )}

        {checkpointsOpen && (
          <CheckpointInspector
            checkpoints={checkpoints}
            busy={controlBusy}
            onClose={() => setCheckpointsOpen(false)}
            onFork={(checkpoint) => void forkCheckpoint(checkpoint)}
            onPreview={async (checkpoint) => {
              if (!sessionId) throw new Error('请先选择任务。')
              return lumenApi.previewTurnRestore(sessionId, checkpoint.index)
            }}
            onRestore={async (preview, clientRequestId) => {
              if (sessionId !== preview.session_id) throw new Error('已切换任务，请重新预览。')
              setControlBusy(true)
              try {
                const result = await lumenApi.applyTurnRestore(preview, clientRequestId)
                await refreshSessionCapabilities()
                if (result.failed) throw new Error(`部分恢复失败：${result.failed.resource} · ${result.failed.error}`)
              } finally { setControlBusy(false) }
            }}
          />
        )}

        {transcriptOpen && (
          <TranscriptInspector
            timeline={run.timeline}
            density={run.transcriptDensity}
            onClose={() => setTranscriptOpen(false)}
            onToggleDensity={() => void toggleTranscriptDensity()}
          />
        )}

        {sessionDialog && (
          <SessionManagementDialog
            mode={sessionDialog.mode}
            session={sessionDialog.session}
            busy={sessionManagementBusy}
            onClose={() => setSessionDialog(null)}
            onConfirm={(title) => void commitSessionDialog(title)}
          />
        )}

        {confirmAuto && (
          <AutoModeDialog
            preset={confirmPreset}
            busy={approvalChanging || loading}
            error={autoError}
            onClose={() => setConfirmAuto(null)}
            onConfirm={() => void approveAuto()}
          />
        )}

        {assistantNotice && <p role="status" className="assistant-global-notice">{assistantNotice}</p>}
        {authenticationRequired || connectionError ? <section className="workspace-connection-state" aria-labelledby="connection-title" role="alert">
          {authenticationRequired ? <ShieldCheck size={30} aria-hidden="true" /> : <PlugsConnected size={30} aria-hidden="true" />}
          <h2 id="connection-title">{authenticationRequired ? '重新连接 Lumen' : '暂时无法连接工作区'}</h2>
          <p>{authenticationRequired ? '此浏览器的登录凭据已失效。请从启动 Lumen 时提供的专属链接重新打开工作区。' : connectionError}</p>
          {authenticationRequired && <details><summary>如何获取新的启动链接</summary>
            <p>在工作区目录的终端依次运行以下命令，再打开终端输出的完整链接。停止服务前，请确认没有正在执行的任务。</p>
            <pre><code>{'lumen web --stop\nlumen web --background'}</code></pre>
            <p>首次升级需要重新登录一次；之后同一工作区和地址的刷新、服务重启会保留登录。无痕窗口或清除 Cookie 后需重新登录。</p>
          </details>}
          <button type="button" className="report-reading-button" disabled={loading} onClick={() => setConnectionAttempt(value => value + 1)}>
            <ArrowsClockwise size={17} className={loading ? 'spin' : ''} aria-hidden="true" />{loading ? '正在连接' : '重新连接'}
          </button>
        </section> : loading && !bootstrap ? <div className="loading-state"><CircleNotch size={22} className="spin" /> 正在连接工作区</div>
        : assistantPage !== 'chat' ? <AssistantWorkspace page={assistantPage} initialIntent={automationIntent} objectId={assistantObjectId}
          sourceSessionId={goalSourceSessionId} onGoalCreated={() => {setGoalSourceSessionId(undefined); setAutomationIntent('')}}
          viewState={assistantViews.current} onNavigate={navigateAssistant} onDiscuss={discussion => void discussAssistant(discussion)} onStartIdea={startIdea} onPlanGoal={planGoal} onOpenSession={id => void openSession(id)} /> :
        <div className={`assistant-conversation-layout ${contextResource ? 'has-content' : ''} mobile-${contextTab}`}>
          {assistantContext.length > 0 && <div className="assistant-context-bar">
            <span>{contextResource ? `正在讨论：${contextResource.resource.title} · 版本 ${contextResource.resource.revision}`
              : contextGoal ? `关联目标：${contextGoal.title} · ${goalStatusLabels[contextGoal.status]}${contextGoal.execution ? ` · ${goalExecutionLabels[contextGoal.execution.status]}` : ' · 尚未启动'}` : '关联目标'}</span>
            <button onClick={() => navigateAssistant(assistantContext[0].kind === 'goal' ? 'goals' : 'library', assistantContext[0].id)}>打开详情</button>
            {contextGoal?.execution?.status === 'active' && <button disabled={approvalSubmitting} onClick={() => void (async () => {
              setApprovalSubmitting(true)
              try {const detail = await lumenApi.controlGoal({action: 'pause', goal_id: contextGoal.id, expected_revision: contextGoal.revision, request_id: crypto.randomUUID(), criteria: ''}); setContextGoal(detail.goal)}
              catch (error) {setAssistantNotice(error instanceof Error ? error.message : '暂停失败，请重新读取目标')}
              finally {setApprovalSubmitting(false)}
            })()}>暂停目标推进</button>}
            {contextResource && <button className="assistant-mobile-content-toggle" onClick={() => setContextTab(contextTab === 'content' ? 'discussion' : 'content')}>{contextTab === 'content' ? '返回讨论' : '查看资料'}</button>}
          </div>}
          {contextResource && <aside className="assistant-discussion-content" aria-label="关联资料正文">
            {!contextResource.content.trimStart().startsWith(`# ${contextResource.resource.title}\n`) && <h2>{contextResource.resource.title}</h2>}
            {contextResource.resource.file_path ? <>
              <p>{resourceTypeLabel(contextResource.resource)} · 版本 {contextResource.resource.revision}</p>
              <button className="report-reading-button" onClick={() => setContextFile(contextResource.resource)}>打开文件预览</button>
              {/\.(md|markdown|txt)$/i.test(contextResource.resource.file_path) && <MarkdownMessage content={contextResource.content} />}
            </> : <MarkdownMessage content={contextResource.content} />}
          </aside>}
          {contextFile?.file_path && <DocumentPreview path={contextFile.file_path} resource={contextFile} onClose={() => setContextFile(null)} />}
        <div className={`conversation-stage ${isLanding ? 'is-landing' : ''}`}>
          {isLanding ? (
            <LandingEntry
              stage={appearance.stage}
              onAppearanceChange={appearance.selectStage}
              appearanceNote={appearance.storageAvailable ? undefined : '浏览器无法保存外观，刷新后可能恢复默认。'}
              value={input}
              busy={false}
              queueMode={queueMode}
              slashCommands={slashCommands}
              attachments={attachments}
              imageInputEnabled={bootstrap?.inputModalities.includes('image') ?? false}
              onChange={setInput}
              onQueueModeChange={setQueueMode}
              onAttachmentsChange={setAttachments}
              onSubmit={() => void submit()}
              onCommand={(command, preserveDraft) => void executeCommand(command, preserveDraft)}
              onStop={() => void stop()}
              settingsControl={composerSettings}
              liveControl={(
                <LiveVoiceControls
                  enabled={Boolean(bootstrap?.liveEnabled)}
                  blocked={workspaceBusy}
                  ensureSession={async () => sessionId ?? createSession()}
                  onEnded={(id) => void openSession(id)}
                />
              )}
            />
          ) : (
            <>
              <section
                ref={timelineRef}
                className="timeline"
                aria-live="polite"
                aria-busy={Boolean(run.runId)}
                onScroll={trackTimelineScroll}
              >
                {loading ? (
                  <div className="loading-state"><CircleNotch size={22} className="spin" /> 正在连接工作区</div>
                ) : run.timeline.length === 0 ? (
                  <EmptyWorkspace />
                ) : (
                  groupTimelineByTurn(run.timeline).map((turn, index, turns) => (
                    <ConversationTurn
                      key={turn.id}
                      turn={turn}
                      active={Boolean(run.runId) && index === turns.length - 1
                        && (!turn.user?.interactionId || turn.user.interactionId === run.runId)}
                      clarification={index === turns.length - 1 ? run.pendingClarification : null}
                      clarificationAnswered={index < turns.length - 1}
                      onAnswer={answerClarification}
                      answerDisabled={Boolean(run.runId) || sessionIsArchived || modelChanging || approvalChanging}
                      onEdit={editMessage}
                      editDisabled={messageEditing ? '正在重新生成消息' : workspaceBusy ? '请先停止或等待当前运行结束' : sessionIsArchived ? '恢复任务后可编辑消息' : modelChanging || approvalChanging ? '正在更新设置，请稍候' : undefined}
                      approvalDocked={Boolean(run.runId)}
                      files={sessionFiles.filter(resource => resource.interaction_id === turn.user?.interactionId)}
                      onApproval={decideApproval}
                    />
                  ))
                )}
                {awaitingPlanReview && run.plan && <PlanProposal plan={run.plan} />}
                {!run.runId && publishableInteractionId && contextResource && !contextResource.resource.file_path && contextResource.resource.kind !== 'work_product' && <div className="assistant-answer-actions">
                  <button disabled={publishing} onClick={() => void applyResourceAnswer()}>将最新回答应用为资料新版本</button>
                  <span>核对后应用，原版本会保留。</span>
                </div>}
                <div className="timeline-end" aria-hidden="true" />
              </section>

              {!loading && <ConversationScrollNav containerRef={timelineRef} items={scrollNavItems}
                onNavigate={() => { stickToLatestRef.current = false; setShowJumpToLatest(true) }} />}

              {showJumpToLatest && (
                <button className="jump-to-latest" type="button" onClick={() => scrollToLatest()}>
                  <ArrowDown size={15} weight="bold" />
                  回到底部
                </button>
              )}

              {sessionIsArchived && activeSession ? (
                <div className="archived-session-notice" role="status">
                  <Archive size={18} weight="duotone" />
                  <span>此任务已归档。恢复后才能继续对话。</span>
                  <button
                    type="button"
                    disabled={sessionManagementBusy}
                    onClick={() => void setArchived(activeSession, false)}
                  >
                    <ArrowCounterClockwise size={16} />
                    恢复任务
                  </button>
                </div>
              ) : (
                <footer className="workspace-composer">
                {pendingApprovals.length > 0 && <section className="composer-approvals" aria-label="需要确认的操作">
                  <p role="status">{approvalSubmitting ? '正在提交授权…' : `需要确认 ${pendingApprovals.length} 项操作`}</p>
                  <fieldset disabled={approvalSubmitting}>
                    {pendingApprovals.map(item => <TimelineRow key={`${run.runId}:${item.id}`} item={item} onApproval={decideApproval} />)}
                  </fieldset>
                </section>}
                {approvalError && <p className="approval-error" role="alert">{approvalError}</p>}
                {awaitingPlanReview && run.planReviewRevision && <PlanReview
                  key={`${sessionId}:${run.planReviewRevision}`}
                  revision={run.planReviewRevision} waiting={run.planReviewStatus === 'approved_waiting_to_execute'}
                  busy={planReviewBusy} disabled={workspaceBusy} feedback={planFeedback} onFeedback={setPlanFeedback}
                  onReview={(action) => void reviewPlan(action)}
                />}
                {run.runId && !awaitingPlanReview && collaborationMode === 'default' && hasCurrentPlan && <PlanProgress
                  key={sessionId} plan={run.plan!} running={Boolean(run.runId)} stopping={stopping} />}
                {run.queuedInputs.length > 0 && (
                  <div className="queued-inputs">
                    {run.queuedInputs.map((item) => <span key={item.id}>已排队 · {queueModeLabel(item.mode)} · {item.text}</span>)}
                    <button type="button" onClick={() => void dequeueInputs()}>撤回到输入框</button>
                  </div>
                )}
                {run.inputFeedback && <p className="input-delivery-note" role="status">
                  {run.inputFeedback.mode === 'steer' ? '已送达当前任务' : run.inputFeedback.mode === 'follow_up' ? '已送达下一轮' : '已送达任务'} · {run.inputFeedback.text}
                </p>}
                <Composer
                  value={input}
                  busy={Boolean(run.runId)}
                  stopping={stopping}
                  stopError={stopError}
                  queueMode={queueMode}
                  slashCommands={slashCommands}
                  attachments={attachments}
                  imageInputEnabled={bootstrap?.inputModalities.includes('image') ?? false}
                  onChange={setInput}
                  onQueueModeChange={setQueueMode}
                  onAttachmentsChange={setAttachments}
                  onSubmit={() => void submit()}
                  onStop={() => void stop()}
                  settingsControl={composerSettings}
                  onCommand={(command, preserveDraft) => void executeCommand(command, preserveDraft)}
                  liveControl={(
                    <LiveVoiceControls
                      enabled={Boolean(bootstrap?.liveEnabled)}
                      blocked={Boolean(run.runId)}
                      ensureSession={async () => sessionId ?? createSession()}
                      onEnded={(id) => void openSession(id)}
                    />
                  )}
                />
                {(settingsNotice || bootstrap?.pendingModel) && <div className="composer-context-notice">
                  {bootstrap?.pendingModel && <p className="composer-notice" role="status">
                    已选择 {bootstrap.pendingModel}，当前运行结束后切换；本轮继续使用 {activeModelName}。
                  </p>}
                  {settingsNotice && <p className="composer-notice" role="status">{settingsNotice}</p>}
                </div>}
                </footer>
              )}
            </>
          )}
        </div></div>}
      </section>
      <nav className="assistant-mobile-navigation" aria-label="主导航" inert={sidebarOpen || sessionSearchOpen}>
        {assistantPages.filter(item => item.id !== 'automations').map(item => <button key={item.id} type="button"
          aria-current={assistantPage === item.id ? 'page' : undefined} onClick={() => navigateAssistant(item.id)}>
          <item.icon size={21} weight={assistantPage === item.id ? 'duotone' : 'regular'} aria-hidden="true" />
          <span>{item.label}</span>
        </button>)}
      </nav>
      {sessionSearchOpen && <SessionSearchDialog sessions={sessions} loading={loading} returnFocusRef={sessionSearchReturnFocusRef}
        onClose={() => setSessionSearchOpen(false)} onSelect={(id) => {
          setSessionSearchOpen(false)
          void openSession(id)
        }} />}
    </main></DocumentProvider>
  )
}

type SettingsSection = 'overview' | 'models' | 'extensions' | 'agents' | 'preferences'

interface ModelDraft {
  reasoningProfile: string
  reasoningEffort: ReasoningLevel | null
  reasoningLevels: ReasoningLevel[] | null
  originalName: string | null
  name: string
  id: string
  api: Exclude<ConfiguredModel['api'], null> | ''
  baseUrl: string
  apiKeyEnv: string
  setDefault: boolean
  settings: Record<string, unknown>
  context: Record<string, unknown>
  nativeWebSearch: NonNullable<ConfiguredModel['nativeWebSearch']>
  authKind: ConfiguredModel['authKind']
}

function modelDraft(model: ConfiguredModel): ModelDraft {
  return {
    reasoningProfile: model.reasoningProfile ?? '',
    reasoningEffort: model.reasoningEffort ?? null,
    reasoningLevels: model.reasoningLevels ?? null,
    originalName: model.name,
    name: model.name,
    id: model.id,
    api: model.api ?? '',
    baseUrl: model.baseUrl ?? '',
    apiKeyEnv: model.apiKeyEnv ?? '',
    setDefault: model.isDefault,
    settings: { ...model.settings },
    context: { ...model.context },
    nativeWebSearch: {
      mode: model.nativeWebSearch?.mode ?? 'auto',
      search_context_size: model.nativeWebSearch?.search_context_size ?? 'medium',
    },
    authKind: model.authKind,
  }
}

function emptyModelDraft(): ModelDraft {
  return {
    reasoningProfile: '',
    reasoningEffort: null,
    reasoningLevels: null,
    originalName: null,
    name: '',
    id: '',
    api: '',
    baseUrl: '',
    apiKeyEnv: '',
    setDefault: false,
    settings: {},
    context: {},
    nativeWebSearch: { mode: 'auto', search_context_size: 'medium' },
    authKind: 'none',
  }
}

function modelDraftChanged(draft: ModelDraft | null, configuration: ConfigurationSnapshot | null) {
  if (!draft) return false
  if (draft.originalName === null) {
    return Boolean(
      draft.name.trim()
      || draft.id.trim()
      || draft.api
      || draft.baseUrl.trim()
      || draft.apiKeyEnv.trim()
      || draft.setDefault
      || draft.reasoningEffort
      || draft.reasoningProfile
      || Object.keys(draft.settings).some((key) => draft.settings[key] !== undefined)
      || Object.keys(draft.context).some((key) => draft.context[key] !== undefined)
      || draft.nativeWebSearch.mode !== 'auto'
      || draft.nativeWebSearch.search_context_size !== 'medium',
    )
  }
  const original = configuration?.models.find((model) => model.name === draft.originalName)
  if (!original) return true
  return (
    draft.name !== original.name
    || draft.id !== original.id
    || draft.api !== (original.api ?? '')
    || draft.baseUrl !== (original.baseUrl ?? '')
    || draft.apiKeyEnv !== (original.apiKeyEnv ?? '')
    || draft.setDefault !== original.isDefault
    || JSON.stringify(draft.settings) !== JSON.stringify(original.settings)
    || JSON.stringify(draft.context) !== JSON.stringify(original.context)
    || JSON.stringify(draft.nativeWebSearch) !== JSON.stringify({
      mode: original.nativeWebSearch?.mode ?? 'auto',
      search_context_size: original.nativeWebSearch?.search_context_size ?? 'medium',
    })
    || draft.reasoningEffort !== (original.reasoningEffort ?? null)
    || draft.reasoningProfile !== (original.reasoningProfile ?? '')
  )
}

function SettingsDialog({
  workspace,
  runActive,
  onClose,
  onModelConfigurationApplied,
}: {
  workspace: string
  runActive: boolean
  onClose: () => void
  onModelConfigurationApplied: () => void
}) {
  const [section, setSection] = useState<SettingsSection>('models')
  const [configuration, setConfiguration] = useState<ConfigurationSnapshot | null>(null)
  const [capabilities, setCapabilities] = useState<CapabilityInventory | null>(null)
  const [draft, setDraft] = useState<ModelDraft | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [restartRequired, setRestartRequired] = useState(false)
  const [deleteArmed, setDeleteArmed] = useState(false)
  const [closeArmed, setCloseArmed] = useState(false)
  const [pathCopied, setPathCopied] = useState(false)
  const [capabilityError, setCapabilityError] = useState('')
  const [capabilityLoading, setCapabilityLoading] = useState(true)
  const [mcpBusy, setMcpBusy] = useState<string | null>(null)
  const [mcpError, setMcpError] = useState('')
  const [draftReasoning, setDraftReasoning] = useState<{ key: string; value: ReasoningSelection } | null>(null)
  const [reasoningError, setReasoningError] = useState('')
  const reasoningQuery = draft && configuration ? JSON.stringify({
    expectedRevision: configuration.revision, id: draft.id, api: draft.api || null,
    baseUrl: draft.baseUrl || null, reasoningLevels: draft.reasoningLevels, settings: draft.settings,
    reasoningProfile: draft.reasoningProfile.trim() || null,
  }) : ''
  const availableReasoning = draftReasoning?.key === reasoningQuery ? draftReasoning.value : null
  useEffect(() => {
    let current = true
    setReasoningError('')
    if (!reasoningQuery) return
    void lumenApi.inspectReasoning(JSON.parse(reasoningQuery)).then((result) => {
      if (current) setDraftReasoning({ key: reasoningQuery, value: result.reasoning })
    }).catch(() => {
      if (current) setReasoningError('无法确认此配置的推理能力，请检查模型 ID、协议和地址。')
    })
    return () => { current = false }
  }, [reasoningQuery])
  const settingsContentRef = useRef<HTMLDivElement>(null)
  const closeNoticeRef = useRef<HTMLDivElement>(null)

  useEffect(() => { settingsContentRef.current?.scrollTo({ top: 0 }) }, [section])
  useEffect(() => {
    if (!closeArmed) return
    closeNoticeRef.current?.scrollIntoView({ block: 'nearest' })
    closeNoticeRef.current?.querySelector('button')?.focus({ preventScroll: true })
  }, [closeArmed])

  const load = useCallback(async () => {
    setLoading(true)
    setError('')
    try {
      const nextConfiguration = await lumenApi.configuration()
      setConfiguration(nextConfiguration)
      setDraft((current) => {
        if (current?.originalName === null) return current
        const selected = nextConfiguration.models.find(
          (model) => model.name === current?.originalName,
        ) ?? nextConfiguration.models[0]
        return selected ? modelDraft(selected) : emptyModelDraft()
      })
    } catch (loadError) {
      setError(loadError instanceof Error ? loadError.message : '无法读取设置')
    } finally {
      setLoading(false)
    }
  }, [])

  const loadCapabilities = useCallback(async () => {
    setCapabilityLoading(true)
    setCapabilityError('')
    try {
      setCapabilities(await lumenApi.capabilities())
    } catch (loadError) {
      setCapabilityError(loadError instanceof Error ? loadError.message : '无法读取运行能力')
    } finally {
      setCapabilityLoading(false)
    }
  }, [])

  useEffect(() => {
    void load()
    void loadCapabilities()
  }, [load, loadCapabilities])

  useEffect(() => {
    if (!draft && configuration?.models[0]) setDraft(modelDraft(configuration.models[0]))
  }, [configuration, draft])

  const save = async () => {
    if (!configuration || !draft) return
    setBusy(true)
    setError('')
    try {
      const result = await lumenApi.upsertModelConfiguration(draft.name.trim(), {
        expectedRevision: configuration.revision,
        id: draft.id.trim(),
        api: draft.api || null,
        baseUrl: draft.baseUrl.trim() || null,
        apiKeyEnv: draft.apiKeyEnv.trim() || null,
        settings: draft.settings,
        reasoningEffort: draft.reasoningEffort,
        reasoningLevels: draft.reasoningLevels,
        reasoningProfile: draft.reasoningProfile.trim() || null,
        context: draft.context,
        nativeWebSearch: draft.nativeWebSearch,
        setDefault: draft.setDefault,
      })
      setConfiguration(result)
      const saved = result.models.find((model) => model.name === draft.name.trim())
      if (saved) setDraft(modelDraft(saved))
      setRestartRequired(Boolean(result.restartRequired))
      if (result.restartRequired === false) onModelConfigurationApplied()
      setDeleteArmed(false)
      setCloseArmed(false)
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : '保存模型失败')
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!configuration || !draft?.originalName) return
    setBusy(true)
    setError('')
    try {
      const result = await lumenApi.deleteModelConfiguration(
        draft.originalName,
        configuration.revision,
      )
      setConfiguration(result)
      setDraft(result.models[0] ? modelDraft(result.models[0]) : emptyModelDraft())
      setRestartRequired(Boolean(result.restartRequired))
      if (result.restartRequired === false) onModelConfigurationApplied()
      setDeleteArmed(false)
      setCloseArmed(false)
    } catch (removeError) {
      setError(removeError instanceof Error ? removeError.message : '删除模型失败')
    } finally {
      setBusy(false)
    }
  }

  const setMcpEnabled = async (name: string, enabled: boolean) => {
    if (!configuration) return
    setMcpBusy(name)
    setMcpError('')
    try {
      const result = await lumenApi.setMcpServerEnabled(name, configuration.revision, enabled)
      setConfiguration(result)
      setRestartRequired(Boolean(result.restartRequired))
    } catch (saveError) {
      setMcpError(saveError instanceof Error ? saveError.message : '保存 MCP 配置失败')
    } finally {
      setMcpBusy(null)
    }
  }

  const valid = Boolean(
    draft?.name.trim() &&
    draft.id.trim() &&
    !(draft.authKind === 'inline' && !draft.apiKeyEnv.trim()),
  )
  const editable = Boolean(configuration?.editable) && !runActive
  const draftChanged = modelDraftChanged(draft, configuration)
  const requestClose = useCallback(() => {
    if (busy) return
    if (draftChanged) {
      setCloseArmed(true)
      closeNoticeRef.current?.scrollIntoView({ block: 'nearest' })
      return
    }
    onClose()
  }, [busy, draftChanged, onClose])
  const dialogRef = useModalFocus(requestClose)

  const navItems: Array<{
    id: SettingsSection
    label: string
    icon: typeof GearSix
  }> = [
    { id: 'overview', label: '通用设置', icon: GearSix },
    { id: 'models', label: '模型', icon: HardDrives },
    {
      id: 'extensions',
      label: '扩展能力',
      icon: PlugsConnected,
    },
    { id: 'agents', label: 'Agent 预设', icon: Robot },
    { id: 'preferences', label: '个性化偏好', icon: Sparkle },
  ]

  return (
    <div
      ref={dialogRef}
      className="runtime-overlay settings-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="settings-title"
      tabIndex={-1}
    >
      <section className="settings-dialog">
        <div className="settings-layout">
          <aside className="settings-sidebar">
            <header className="settings-header">
              <button type="button" className="settings-close" aria-label="关闭设置" disabled={busy} onClick={requestClose}>
                <X size={22} aria-hidden="true" />
              </button>
              <strong id="settings-title">设置</strong>
            </header>
          <nav className="settings-nav" aria-label="设置分类">
            {navItems.map((item) => {
              const Icon = item.icon
              return (
                <button
                  key={item.id}
                  type="button"
                  className={section === item.id ? 'is-active' : ''}
                  aria-current={section === item.id ? 'page' : undefined}
                  onClick={() => setSection(item.id)}
                >
                  <Icon size={20} aria-hidden="true" />
                  <span>{item.label}</span>
                </button>
              )
            })}
          </nav>
          <div className="settings-workspace">
            <span title={workspace}>{workspace.split('/').filter(Boolean).at(-1) || 'Lumen 工作区'}</span>
            {configuration?.targetPath && (
              <button
                type="button"
                className="settings-path-button"
                aria-label={pathCopied ? '配置路径已复制' : '复制配置路径'}
                onClick={() => {
                  void navigator.clipboard.writeText(configuration.targetPath).then(() => {
                    setPathCopied(true)
                    window.setTimeout(() => setPathCopied(false), 1200)
                  }).catch(() => setError('无法复制配置路径，请检查浏览器剪贴板权限。'))
                }}
              >
                {pathCopied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
                {pathCopied ? '已复制' : '复制配置路径'}
              </button>
            )}
          </div>
          </aside>
          <div className="settings-content" ref={settingsContentRef}>
            {closeArmed && draftChanged && (
              <div className="settings-notice is-warning" role="alert" ref={closeNoticeRef}>
                <WarningCircle size={17} />
                <span><strong>有尚未保存的更改</strong>关闭设置会放弃当前模型的修改。</span>
                <div>
                  <button type="button" onClick={() => { setSection('models'); setCloseArmed(false) }}>继续编辑</button>
                  <button type="button" className="is-danger" onClick={onClose}>放弃更改</button>
                </div>
              </div>
            )}
            {loading ? (
              <div className="settings-loading"><CircleNotch size={18} className="spin" /> 正在读取配置</div>
            ) : error && !configuration ? (
              <div className="settings-error" role="alert">
                <WarningCircle size={18} />
                <span>{error}</span>
                <button type="button" onClick={() => void load()}>重试</button>
              </div>
            ) : section === 'preferences' ? <PersonalPreferences /> : section === 'overview' ? (
              <SettingsOverview
                configuration={configuration}
                capabilities={capabilities}
                capabilityError={capabilityError}
              />
            ) : section === 'models' ? (
              <div className="model-settings">
                <div className="settings-section-heading">
                  <div><h2>模型</h2><p>配置推理提供方；密钥只通过环境变量读取。</p></div>
                  <button
                    type="button"
                    disabled={!editable || busy || draftChanged}
                    title={draftChanged ? '请先保存或还原当前更改' : undefined}
                    onClick={() => {
                      setDraft(emptyModelDraft())
                      setDeleteArmed(false)
                      setCloseArmed(false)
                    }}
                  >
                    <Plus size={14} /> 添加模型
                  </button>
                </div>
                {(restartRequired || configuration?.restartRequired) && (
                  <div className="settings-notice is-success" role="status">
                    <CheckCircle size={17} weight="fill" />
                    <span><strong>配置已保存</strong>重启 Lumen Web 后载入新的模型注册表。</span>
                  </div>
                )}
                {!restartRequired && configuration?.restartRequired === false && (
                  <div className="settings-notice is-success" role="status">
                    <CheckCircle size={17} weight="fill" />
                    <span><strong>配置已生效</strong>模型注册表与当前 Runtime 已动态更新，无需重启。</span>
                  </div>
                )}
                {runActive && (
                  <div className="settings-notice"><Info size={17} /><span>任务运行期间配置为只读，请在运行结束后保存。</span></div>
                )}
                {!configuration?.editable && (
                  <div className="settings-notice"><Info size={17} /><span>{configuration?.editReason}</span></div>
                )}
                <div className="model-settings-body">
                  <label className="settings-model-picker">
                    <span>当前配置</span>
                    <select aria-label="已配置模型" value={draft?.originalName ?? ''} disabled={busy || draftChanged}
                      onChange={(event) => {
                        const selected = configuration?.models.find((model) => model.name === event.target.value)
                        if (selected) setDraft(modelDraft(selected))
                        setDeleteArmed(false)
                        setCloseArmed(false)
                      }}>
                      {draft?.originalName === null && <option value="">新模型</option>}
                      {configuration?.models.map((model) => <option key={model.name} value={model.name}>{model.name}{configuration.activeModel === model.name ? ' · 当前' : model.isDefault ? ' · 默认' : ''}</option>)}
                    </select>
                  </label>
                  {draft && (
                    <form className="model-form" onSubmit={(event) => { event.preventDefault(); void save() }}>
                      {error && <div className="settings-inline-error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>重新读取</button></div>}
                      <fieldset className="model-form-grid">
                        <legend>连接设置</legend>
                        <label><span>配置名称</span><input value={draft.name} disabled={draft.originalName !== null || busy || !editable} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如 local-qwen" /></label>
                        <label><span>模型 ID</span><input value={draft.id} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, id: event.target.value })} placeholder="例如 openai:qwen3" /></label>
                        <label><span>API 协议</span><select value={draft.api} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, api: event.target.value as ModelDraft['api'] })}><option value="">自动选择（默认 Responses）</option><option value="responses">Responses API</option><option value="chat">Chat Completions</option><option value="openai-responses">OpenAI Responses 兼容</option><option value="openai-completions">OpenAI Completions 兼容</option><option value="chat-completions">Chat Completions 兼容别名</option></select></label>
                        <label><span>API 地址</span><input value={draft.baseUrl} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} placeholder="提供方默认或 http://127.0.0.1:11434/v1" /></label>
                        <label className="model-form-wide"><span>API 密钥环境变量</span><input value={draft.apiKeyEnv} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, apiKeyEnv: event.target.value })} placeholder="例如 OPENAI_API_KEY；本地模型可留空" /><small>Web 不读取、显示或保存密钥明文。</small></label>
                      </fieldset>
                      <fieldset className="model-form-grid">
                        <legend>推理与搜索</legend>
                        <label><span>默认推理强度</span><select aria-label="默认推理强度"
                          value={draft.reasoningEffort ?? ''}
                          disabled={busy || !editable || !availableReasoning}
                          onChange={(event) => setDraft({ ...draft,
                            reasoningEffort: (event.target.value || null) as ReasoningLevel | null })}>
                          <option value="">沿用原始配置</option>
                          {draft.reasoningEffort && !availableReasoning?.supported_levels.includes(draft.reasoningEffort)
                            && <option value={draft.reasoningEffort}>{draft.reasoningEffort}（待校验）</option>}
                          {availableReasoning && reasoningOptions(availableReasoning, draft.reasoningEffort)
                            .map((option) => <option key={option.value} value={option.value}>
                              {option.label}
                            </option>)}
                        </select><small>{reasoningError || (!availableReasoning ? '正在确认可用档位…'
                          : availableReasoning.capability_status === 'unknown' ? '此模型和协议的推理控制未配置。'
                            : availableReasoning.capability_status === 'unsupported' ? '此模型不支持调节推理强度。'
                              : `能力来源：${availableReasoning.capability_source ?? '未提供'}。箭头表示实际映射强度。`)}</small>
                          {availableReasoning?.capability_reviewed_on && <small>
                            核对日期：{availableReasoning.capability_reviewed_on} · 目录：{availableReasoning.catalog_revision}
                          </small>}
                        </label>
                        <label className="model-form-wide"><span>代理能力 Profile（可选）</span>
                          <input aria-label="代理能力 Profile" value={draft.reasoningProfile}
                            disabled={busy || !editable} placeholder="留空自动匹配；例如 openai-gpt56-sol"
                            onChange={(event) => setDraft({ ...draft, reasoningProfile: event.target.value })} />
                          <small>自定义代理可引用已核对的目录规则；模型 ID 和 API 协议必须与规则一致。</small>
                        </label>
                        <label><span>模型内建联网</span><select aria-label="模型内建联网"
                          value={draft.nativeWebSearch.mode ?? 'auto'} disabled={busy || !editable}
                          onChange={(event) => setDraft({ ...draft, nativeWebSearch: {
                            ...draft.nativeWebSearch,
                            mode: event.target.value as 'auto' | 'enabled' | 'disabled',
                          } })}>
                          <option value="auto">自动识别</option>
                          <option value="enabled">始终开启</option>
                          <option value="disabled">关闭</option>
                        </select><small>{draft.nativeWebSearch.mode === 'enabled'
                          ? '每次请求声明由模型提供方执行的 web_search；OpenAI 兼容模型需使用 Responses。'
                          : draft.nativeWebSearch.mode === 'disabled'
                            ? '不向此模型声明内建联网工具。外部 MCP 开关不受影响。'
                            : '已核对的 DeepSeek Flash Responses 默认开启；未知端点保持关闭，可显式开启。'}</small></label>
                        <label><span>搜索上下文</span><select aria-label="模型搜索上下文"
                          value={draft.nativeWebSearch.search_context_size ?? 'medium'}
                          disabled={busy || !editable || draft.nativeWebSearch.mode === 'disabled'}
                          onChange={(event) => setDraft({ ...draft, nativeWebSearch: {
                            ...draft.nativeWebSearch,
                            search_context_size: event.target.value as 'low' | 'medium' | 'high',
                          } })}>
                          <option value="low">精简</option><option value="medium">标准</option><option value="high">深入</option>
                        </select><small>提供方可忽略此提示；DeepSeek 当前固定由服务端决定搜索上下文。</small></label>
                      </fieldset>
                      <fieldset className="model-form-grid">
                        <legend>用量限制</legend>
                        <label><span>最大输出 Token</span><input type="number" min="1" value={String(draft.settings.max_tokens ?? '')} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, settings: { ...draft.settings, max_tokens: event.target.value ? Number(event.target.value) : undefined } })} placeholder="提供方默认" /></label>
                        <label><span>上下文窗口</span><input type="number" min="1" value={String(draft.context.window_tokens ?? '')} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, context: { ...draft.context, window_tokens: event.target.value ? Number(event.target.value) : undefined } })} placeholder="自动识别" /></label>
                      </fieldset>
                      {draft.authKind === 'inline' && !draft.apiKeyEnv.trim() && (
                        <div className="settings-notice"><WarningCircle size={17} /><span>此模型来自含内联密钥的配置。先改为环境变量名，Web 才会创建安全覆盖。</span></div>
                      )}
                      <label className="settings-checkbox"><span>{draft.originalName && draft.setDefault ? '当前默认模型' : '设为默认并立即切换'}</span><input type="checkbox" role="switch" checked={draft.setDefault} disabled={busy || !editable || Boolean(draft.originalName && draft.setDefault)} onChange={(event) => setDraft({ ...draft, setDefault: event.target.checked })} /></label>
                      <footer>
                        {draft.originalName && configuration && configuration.models.length > 1 && (
                          <button
                            type="button"
                            className={deleteArmed ? 'is-danger' : 'is-quiet'}
                            disabled={busy || !editable}
                            onClick={() => deleteArmed ? void remove() : setDeleteArmed(true)}
                          >
                            <Trash size={14} /> {deleteArmed ? '确认删除' : '删除模型'}
                          </button>
                        )}
                        <span />
                        <button
                          type="button"
                          className="is-quiet"
                          disabled={busy || !draftChanged}
                          onClick={() => {
                            const original = configuration?.models.find(
                              (model) => model.name === draft.originalName,
                            )
                            setDraft(original ? modelDraft(original) : emptyModelDraft())
                            setCloseArmed(false)
                            setDeleteArmed(false)
                          }}
                        >
                          {draft.originalName ? '还原' : '清空'}
                        </button>
                        <button type="submit" disabled={!valid || !editable || busy || !draftChanged}>{busy ? '正在保存…' : draftChanged ? '保存配置' : '已保存'}</button>
                      </footer>
                    </form>
                  )}
                </div>
              </div>
            ) : section === 'extensions' ? (
              <CapabilitySettings
                capabilities={capabilities}
                configuration={configuration}
                loading={capabilityLoading}
                error={capabilityError}
                mutationError={mcpError}
                editable={editable}
                busy={mcpBusy}
                restartRequired={restartRequired || Boolean(configuration?.restartRequired)}
                onRetry={() => void loadCapabilities()}
                onMcpEnabled={(name, enabled) => void setMcpEnabled(name, enabled)}
              />
            ) : (
              <AgentProfileSettings
                capabilities={capabilities}
                loading={capabilityLoading}
                error={capabilityError}
                onRetry={() => void loadCapabilities()}
              />
            )}
          </div>
        </div>
      </section>
    </div>
  )
}

function SettingsOverview({
  configuration,
  capabilities,
  capabilityError,
}: {
  configuration: ConfigurationSnapshot | null
  capabilities: CapabilityInventory | null
  capabilityError: string
}) {
  return (
    <div className="settings-overview">
      <div className="settings-section-heading"><div><h2>通用设置</h2><p>当前工作区的配置来源与运行能力概览。</p></div></div>
      {capabilityError && <div className="settings-notice"><WarningCircle size={17} /><span><strong>运行能力暂不可用</strong>{capabilityError}。模型配置仍可独立管理。</span></div>}
      {configuration?.warnings.map((warning) => <div className="settings-notice" key={warning}><Info size={17} /><span>{warning}</span></div>)}
      <dl className="settings-stat-grid">
        <div><dt>模型</dt><dd>{configuration?.models.length ?? 0}</dd></div>
        <div><dt>工具</dt><dd>{capabilities?.tools.length ?? 0}</dd></div>
        <div><dt>Skills</dt><dd>{capabilities?.skills.length ?? 0}</dd></div>
        <div><dt>MCP</dt><dd>{capabilities?.mcp_servers.length ?? 0}</dd></div>
      </dl>
      <section className="settings-source-list">
        <h3>配置优先级</h3>
        {configuration?.sources.map((source) => (
          <div key={`${source.scope}:${source.path}`}><span>{source.scope}</span><code>{source.path}</code></div>
        ))}
        <div className="is-target"><span>Web 写入</span><code>{configuration?.targetPath}</code></div>
      </section>
    </div>
  )
}

function CapabilitySettings({
  capabilities,
  configuration,
  loading,
  error,
  mutationError,
  editable,
  busy,
  restartRequired,
  onRetry,
  onMcpEnabled,
}: {
  capabilities: CapabilityInventory | null
  configuration: ConfigurationSnapshot | null
  loading: boolean
  error: string
  mutationError: string
  editable: boolean
  busy: string | null
  restartRequired: boolean
  onRetry: () => void
  onMcpEnabled: (name: string, enabled: boolean) => void
}) {
  const runtimeMcp = new Map(
    (capabilities?.mcp_servers ?? []).map((item) => [String(item.name), item]),
  )
  const configuredMcp = configuration?.mcpServers ?? []
  const groups = [
    { title: 'Skills', items: capabilities?.skills ?? [], key: 'name' },
    { title: '工具', items: capabilities?.tools ?? [], key: 'name' },
  ]
  return <div className="capability-settings"><div className="settings-section-heading"><div><h2>扩展能力</h2><p>模型内建联网在模型页配置；这里控制 Exa 等外部 MCP 连接。</p></div></div>{restartRequired && <div className="settings-notice is-success" role="status"><CheckCircle size={17} weight="fill" /><span><strong>扩展配置已保存</strong>重启 Lumen Web 后应用 MCP 连接变化。</span></div>}{mutationError && <div className="settings-inline-error" role="alert"><span>{mutationError}</span></div>}{loading ? <div className="settings-loading"><CircleNotch size={18} className="spin" /> 正在读取运行能力</div> : error ? <div className="settings-error" role="alert"><WarningCircle size={18} /><span>{error}</span><button type="button" onClick={onRetry}>重试</button></div> : <><section className="mcp-settings-list"><h3>MCP Servers <small>{configuredMcp.length}</small></h3>{configuredMcp.length ? configuredMcp.map((server) => {
    const runtime = runtimeMcp.get(server.name)
    const status = String(runtime?.status ?? (server.enabled ? '等待重启' : 'disabled'))
    return <div className="capability-row capability-toggle" key={server.name}><span><strong>{server.name}</strong><small>{server.enabled ? `配置为启用 · 当前状态：${status}` : '配置为停用 · 不连接、不加载工具与内容'}</small></span><label className="settings-checkbox"><span className="sr-only">{server.enabled ? `停用 ${server.name}` : `启用 ${server.name}`}</span><input type="checkbox" role="switch" aria-label={`${server.name} MCP`} checked={server.enabled} disabled={!editable || busy !== null} onChange={(event) => onMcpEnabled(server.name, event.target.checked)} /></label></div>
  }) : <p>当前没有 MCP Server。</p>}</section>{groups.map((group) => <section key={group.title}><h3>{group.title} <small>{group.items.length}</small></h3>{group.items.length ? group.items.map((item, index) => <div className="capability-row" key={`${String(item[group.key] ?? index)}`}><span><strong>{String(item.name ?? item.reference ?? `item-${index + 1}`)}</strong><small>{String(item.description ?? item.origin ?? '')}</small></span><em>{String(item.status ?? '已载入')}</em></div>) : <p>当前没有 {group.title}。</p>}</section>)}</>}</div>
}

function AgentProfileSettings({
  capabilities,
  loading,
  error,
  onRetry,
}: {
  capabilities: CapabilityInventory | null
  loading: boolean
  error: string
  onRetry: () => void
}) {
  const profiles = capabilities?.agent_profiles ?? []
  return <div className="capability-settings"><div className="settings-section-heading"><div><h2>Agent 预设</h2><p>展示当前发现的角色配置及其能力收窄范围。</p></div></div>{loading ? <div className="settings-loading"><CircleNotch size={18} className="spin" /> 正在读取 Agent 预设</div> : error ? <div className="settings-error" role="alert"><WarningCircle size={18} /><span>{error}</span><button type="button" onClick={onRetry}>重试</button></div> : <section><h3>可用预设 <small>{profiles.length}</small></h3>{profiles.length ? profiles.map((profile, index) => <div className="capability-row" key={String(profile.name ?? index)}><span><strong>{String(profile.name ?? `profile-${index + 1}`)}</strong><small>{String(profile.description ?? profile.role ?? '')}</small></span><em>{String(profile.status ?? '可用')}</em></div>) : <p>当前没有自定义 Agent 预设。</p>}</section>}</div>
}

function SessionManagementDialog({
  mode,
  session,
  busy,
  onClose,
  onConfirm,
}: {
  mode: 'rename' | 'delete'
  session: SessionSummary
  busy: boolean
  onClose: () => void
  onConfirm: (title?: string) => void
}) {
  const [title, setTitle] = useState(session.title)
  const dialogRef = useModalFocus(busy ? () => undefined : onClose)
  const validTitle = title.trim().length > 0 && title.trim().length <= 80

  return (
    <div
      ref={dialogRef}
      className="runtime-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="session-management-title"
      tabIndex={-1}
    >
      <form
        className="session-management-dialog"
        onSubmit={(event) => {
          event.preventDefault()
          if (mode === 'delete' || validTitle) onConfirm(mode === 'rename' ? title.trim() : undefined)
        }}
      >
        <header>
          <div>
            <strong id="session-management-title">
              {mode === 'rename' ? '重命名任务' : '删除任务'}
            </strong>
            <span title={session.title}>{session.title}</span>
          </div>
          <button type="button" aria-label="关闭" disabled={busy} onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        {mode === 'rename' ? (
          <label>
            <span>任务标题</span>
            <input
              value={title}
              maxLength={80}
              autoFocus
              disabled={busy}
              onChange={(event) => setTitle(event.target.value)}
            />
            <small>{title.length}/80</small>
          </label>
        ) : (
          <p>
            删除后该任务会从 Web 列表中移除，无法在界面恢复。底层 Session journal
            保持追加式审计，不会原地改写历史记录。
          </p>
        )}
        <footer>
          <button type="button" className="quiet" disabled={busy} onClick={onClose}>取消</button>
          <button
            type="submit"
            className={mode === 'delete' ? 'danger' : ''}
            disabled={busy || (mode === 'rename' && !validTitle)}
          >
            {busy ? '正在处理…' : mode === 'rename' ? '保存标题' : '确认删除'}
          </button>
        </footer>
      </form>
    </div>
  )
}

function AutoModeDialog({ preset, busy, error, onClose, onConfirm }: {
  preset: PermissionPreset | null
  busy: boolean
  error: string
  onClose: () => void
  onConfirm: () => void
}) {
  const dialogRef = useModalFocus(busy ? () => undefined : onClose)
  return (
    <div
      ref={dialogRef}
      className="runtime-overlay auto-confirm-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="auto-confirm-title"
      tabIndex={-1}
    >
      <section className="session-management-dialog auto-confirm-dialog">
        <header>
          <div>
            <strong id="auto-confirm-title">{preset === 'full_access' ? '允许完全访问本机？' : '开启自动执行？'}</strong>
          </div>
          <button type="button" aria-label="关闭" disabled={busy} onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        <div className="auto-confirm-content">
          <ShieldCheck size={20} aria-hidden="true" />
          <div>
            <strong>{preset === 'full_access' ? '当前对话可操作其他本地目录' : '减少逐次确认'}</strong>
            <p>{preset === 'full_access'
              ? '可读取、修改当前用户有权限访问的本机文件，命令可联网，常规操作自动执行。内建提交、推送仍每次确认。仅影响当前对话，可在运行结束后切回工作区范围。'
              : preset === 'workspace_auto'
                ? '常规操作在工作区沙箱内自动执行，命令联网沿用工作区配置。内建提交、推送仍每次确认。'
                : '仅改变审批策略，保留当前访问范围。已分类的写入、执行和外部工具自动运行；内建提交、推送仍每次确认。'}</p>
          </div>
        </div>
        {error && <div className="auto-confirm-error" role="alert"><WarningCircle size={16} aria-hidden="true" />{error}</div>}
        <footer>
          <button type="button" className="quiet" disabled={busy} onClick={onClose}>取消</button>
          <button type="button" disabled={busy} onClick={onConfirm}>{busy ? '正在切换…' : preset === 'full_access' ? '允许完全访问' : '确认开启'}</button>
        </footer>
      </section>
    </div>
  )
}

function RuntimeInspector({
  agents,
  workProducts,
  pendingEffects,
  recoverableEffects,
  busy,
  onClose,
  onRefresh,
  onAgentAction,
  onWaive,
}: {
  agents: AgentRecord[]
  workProducts: Array<Record<string, unknown>>
  pendingEffects: Array<Record<string, unknown>>
  recoverableEffects: Array<Record<string, unknown>>
  busy: boolean
  onClose: () => void
  onRefresh: () => void
  onAgentAction: (agent: AgentRecord, action: string) => void
  onWaive: (effect: Record<string, unknown>, reason: string) => Promise<void>
}) {
  const dialogRef = useModalFocus(onClose)
  return (
    <div ref={dialogRef} className="runtime-overlay" role="dialog" aria-modal="true" aria-label="智能体与工作状态" tabIndex={-1}>
      <section className="runtime-inspector">
        <header>
          <div><strong>运行时</strong><span>智能体、工作对象与待处理副作用</span></div>
          <div className="runtime-header-actions">
            <button type="button" aria-label="刷新运行时状态" data-tooltip="刷新" onClick={onRefresh} disabled={busy}>
              <ArrowsClockwise size={16} className={busy ? 'spin' : ''} aria-hidden="true" />
            </button>
            <button type="button" aria-label="关闭" data-tooltip="关闭" onClick={onClose}>
              <X size={16} aria-hidden="true" />
            </button>
          </div>
        </header>
        <div className="runtime-section">
          <h2>智能体 <small>{agents.length}</small></h2>
          {agents.length === 0 ? <p className="runtime-empty">当前 Session 没有 Agent。</p> : agents.map((agent) => (
            <article className="agent-record" key={agent.id}>
              <div>
                <strong>{agent.path ?? agent.id}</strong>
                <span>{agent.role ?? 'default'} · {agent.status}</span>
                {agent.task && <p>{agent.task}</p>}
                {agent.result_summary && <p>{agent.result_summary}</p>}
              </div>
              <div className="runtime-actions">
                <button type="button" disabled={busy} onClick={() => onAgentAction(agent, 'send_message')}>消息</button>
                <button type="button" disabled={busy} onClick={() => onAgentAction(agent, 'continue')}>继续</button>
                {['queued', 'running', 'waiting', 'approval_pending'].includes(agent.status) && (
                  <button type="button" disabled={busy} onClick={() => onAgentAction(agent, 'interrupt')}>中断</button>
                )}
                {agent.status === 'import_pending' && <>
                  <button type="button" disabled={busy} onClick={() => onAgentAction(agent, 'approve_import')}>导入</button>
                  <button type="button" disabled={busy} onClick={() => onAgentAction(agent, 'reject_import')}>拒绝</button>
                </>}
                {!['queued', 'running', 'waiting', 'approval_pending', 'import_pending', 'closed'].includes(agent.status) && (
                  <button type="button" disabled={busy} onClick={() => onAgentAction(agent, 'close')}>关闭</button>
                )}
              </div>
            </article>
          ))}
        </div>
        <div className="runtime-section">
          <h2>工作对象 <small>{workProducts.length}</small></h2>
          {workProducts.length === 0 ? <p className="runtime-empty">当前没有持续工作对象。</p> : workProducts.map((item, index) => (
            <article className="work-record" key={String(item.id ?? index)}>
              <strong>{String(item.resource ?? item.id ?? `work-product-${index + 1}`)}</strong>
              <span>{String(item.kind ?? 'resource')} · {String(item.status ?? 'unknown')}</span>
            </article>
          ))}
        </div>
        <div className="runtime-section">
          <h2>待处理副作用 <small>{pendingEffects.length}</small></h2>
          {pendingEffects.length === 0 ? <p className="runtime-empty">没有待验证副作用。</p> : pendingEffects.map((effect, index) => (
            <EffectRecovery key={String(effect.id ?? index)} effect={effect} busy={busy} onConfirm={onWaive} />
          ))}
          {recoverableEffects.length > 0 && <p className="runtime-meta">可恢复 effects：{recoverableEffects.length}</p>}
        </div>
      </section>
    </div>
  )
}

function EffectRecovery({ effect, busy, onConfirm }: {
  effect: Record<string, unknown>
  busy: boolean
  onConfirm: (effect: Record<string, unknown>, reason: string) => Promise<void>
}) {
  const [reason, setReason] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const savingRef = useRef(false)
  return <form className="work-record" onSubmit={async (event) => {
    event.preventDefault()
    if (busy || savingRef.current || !reason.trim()) return
    savingRef.current = true
    setSaving(true)
    setError('')
    try { await onConfirm(effect, reason.trim()) }
    catch (cause) { setError(cause instanceof Error ? cause.message : '记录失败，请重试。') }
    finally { savingRef.current = false; setSaving(false) }
  }}>
    <strong>{String(effect.operation ?? effect.id ?? '待核实操作')}</strong>
    <p>{String(effect.error ?? effect.summary ?? '执行结果尚未验证。')}</p>
    <label>核实依据
      <input value={reason} maxLength={1000} disabled={busy || saving} required
        onChange={(event) => setReason(event.target.value)} placeholder="说明已检查的结果及可接受的依据" />
    </label>
    <p>仅确认这一次操作，不重试外部调用，也不改变后续工具权限。</p>
    {error && <p role="alert">{error}</p>}
    <button type="submit" disabled={busy || saving || !reason.trim()}>
      {saving ? '正在记录…' : '记录人工确认'}
    </button>
  </form>
}

function CheckpointInspector({
  checkpoints,
  busy,
  onClose,
  onFork,
  onPreview,
  onRestore,
}: {
  checkpoints: CheckpointRecord[]
  busy: boolean
  onClose: () => void
  onFork: (checkpoint: CheckpointRecord) => void
  onPreview: (checkpoint: CheckpointRecord) => Promise<TurnRestorePreview>
  onRestore: (preview: TurnRestorePreview, clientRequestId: string) => Promise<void>
}) {
  const dialogRef = useModalFocus(onClose)
  const [preview, setPreview] = useState<TurnRestorePreview | null>(null)
  const [restoreRequestId, setRestoreRequestId] = useState('')
  const [loading, setLoading] = useState(false)
  const [notice, setNotice] = useState('')
  const inspect = async (checkpoint: CheckpointRecord) => {
    setLoading(true); setNotice(''); setPreview(null)
    try { setPreview(await onPreview(checkpoint)); setRestoreRequestId(requestId()) }
    catch (error) { setNotice(error instanceof Error ? error.message : '无法预览恢复。') }
    finally { setLoading(false) }
  }
  const restore = async () => {
    if (!preview) return
    setLoading(true); setNotice('')
    try { await onRestore(preview, restoreRequestId); setPreview(null); setNotice('文件已恢复；对话保持当前状态。') }
    catch (error) { setNotice(error instanceof Error ? error.message : '恢复失败，请刷新检查文件。') }
    finally { setLoading(false) }
  }
  return (
    <div ref={dialogRef} className="runtime-overlay" role="dialog" aria-modal="true" aria-label="Session 检查点" tabIndex={-1}>
      <section className="runtime-inspector compact-inspector">
        <header><div><strong>检查点</strong><span>Rewind 会创建新 Session，不覆盖当前工作区。</span></div><button type="button" aria-label="关闭" onClick={onClose}><X size={16} aria-hidden="true" /></button></header>
        <div className="runtime-section">
          {checkpoints.length === 0 ? <p className="runtime-empty">还没有已完成 turn。</p> : checkpoints.map((checkpoint) => (
            <article className="checkpoint-record" key={checkpoint.index}>
              <div><strong>Turn {checkpoint.index + 1}</strong><span>{checkpoint.status} · {checkpoint.receipt_count} receipts · {checkpoint.mutation_count} mutations</span><p>{checkpoint.prompt}</p></div>
              <button type="button" disabled={busy} onClick={() => onFork(checkpoint)}>从这里分支</button>
              <button type="button" disabled={busy || loading} onClick={() => void inspect(checkpoint)}>预览文件恢复</button>
            </article>
          ))}
          {notice && <p role="status">{notice}</p>}
          {preview && <section aria-label="文件恢复预览">
            <h3>恢复到 Turn {preview.before_turn + 1} 之前</h3>
            <p>仅恢复有快照的文件；对话、Shell 和外部操作需单独处理。</p>
            {preview.files.map((file) => <article key={file.work_product_id}>
              <strong>{file.resource}{file.target_exists ? '' : '（将删除）'}</strong>
              <pre style={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{file.diff}</pre>
              {file.diff_truncated && <p>差异预览已截断。</p>}
            </article>)}
            {preview.blockers.map((item) => <p role="alert" key={item.resource}>{item.resource}：{item.error}</p>)}
            <p>未覆盖操作：{preview.excluded.length}；可恢复文件：{preview.files.length}。</p>
            <button type="button" disabled={busy || loading || !!preview.blockers.length || !preview.files.length}
              onClick={() => void restore()}>确认恢复这些文件</button>
            <button type="button" disabled={loading} onClick={() => setPreview(null)}>取消</button>
          </section>}
        </div>
      </section>
    </div>
  )
}

export function TranscriptInspector({
  timeline,
  density,
  onClose,
  onToggleDensity,
}: {
  timeline: TimelineEntry[]
  density: 'normal' | 'verbose'
  onClose: () => void
  onToggleDensity: () => void
}) {
  const [query, setQuery] = useState('')
  const [copied, setCopied] = useState(false)
  const needle = query.trim().toLowerCase()
  // Normal reading, searching and copying share the conversation projection.
  // Verbose inspection deliberately retains the original protocol records.
  const entries = density === 'verbose' ? timeline : projectThinkingMarkup(timeline)
  const visible = needle
    ? entries.filter((item) => JSON.stringify(item).toLowerCase().includes(needle))
    : entries
  const dialogRef = useModalFocus(onClose)
  const copyVisible = async () => {
    const text = visible.map((item) => (
      density === 'verbose'
        ? JSON.stringify(item, null, 2)
        : `[${transcriptEventLabel(item)}] ${item.toolName ? `${item.toolName} · ` : ''}${item.text || item.preview || item.status || ''}`
    )).join('\n\n')
    await navigator.clipboard.writeText(text)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 1200)
  }
  return (
    <div ref={dialogRef} className="runtime-overlay transcript-overlay" role="dialog" aria-modal="true" aria-labelledby="transcript-title" tabIndex={-1}>
      <section className="runtime-inspector transcript-inspector">
        <header>
          <div><strong id="transcript-title">运行记录</strong><span>{timeline.length} 条事件 · {density === 'verbose' ? '详细视图' : '标准视图'}</span></div>
          <div className="runtime-header-actions">
            <button type="button" aria-label={density === 'verbose' ? '切换到标准视图' : '切换到详细视图'} data-tooltip={density === 'verbose' ? '标准视图' : '详细视图'} aria-pressed={density === 'verbose'} onClick={onToggleDensity}>
              <List size={16} aria-hidden="true" />
            </button>
            <button type="button" aria-label="复制当前记录" data-tooltip={copied ? '已复制' : '复制'} disabled={visible.length === 0} onClick={() => void copyVisible()}>
              {copied ? <Check size={16} aria-hidden="true" /> : <Copy size={16} aria-hidden="true" />}
            </button>
            <button type="button" aria-label="关闭运行记录" data-tooltip="关闭" onClick={onClose}><X size={16} aria-hidden="true" /></button>
          </div>
        </header>
        <div className="transcript-toolbar">
          <label>
            <MagnifyingGlass size={16} aria-hidden="true" />
            <span className="sr-only">搜索运行记录</span>
            <input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索消息、工具或状态" autoFocus />
            {query && <button type="button" aria-label="清除搜索" onClick={() => setQuery('')}><X size={14} aria-hidden="true" /></button>}
          </label>
          <span aria-live="polite">{needle ? `${visible.length} 条匹配` : `${visible.length} 条记录`}</span>
        </div>
        {visible.length ? (
          <ol className="transcript-list">
            {visible.map((item) => <TranscriptRecord key={item.id} item={item} density={density} />)}
          </ol>
        ) : (
          <div className="transcript-empty">
            <strong>没有匹配的记录</strong>
            <span>尝试搜索消息正文、工具名称或执行状态。</span>
            <button type="button" onClick={() => setQuery('')}>清除搜索</button>
          </div>
        )}
      </section>
    </div>
  )
}

function TranscriptRecord({
  item,
  density,
}: {
  item: TimelineEntry
  density: 'normal' | 'verbose'
}) {
  const planSummary = item.kind === 'plan' && item.plan
    ? [
      item.plan.goal,
      item.plan.steps.length ? `${item.plan.steps.length} 个步骤` : '计划状态已更新',
    ].filter(Boolean).join(' · ')
    : ''
  const body = density === 'verbose'
    ? JSON.stringify(item, null, 2)
    : item.text || item.preview || planSummary || item.status || '已记录结构化事件。'
  const preview = body.replace(/\s+/g, ' ').trim()
  const long = density === 'verbose' || body.length > 360 || body.split('\n').length > 7
  const metadata = [item.toolName, item.status].filter(Boolean).join(' · ')

  return (
    <li className={`transcript-record is-${item.kind}`}>
      <header>
        <span className="transcript-kind">
          {item.kind === 'tool' && <TerminalWindow size={14} aria-hidden="true" />}
          {transcriptEventLabel(item)}
        </span>
        {metadata && <small>{metadata}</small>}
      </header>
      {long ? (
        <details>
          <summary>{preview.slice(0, 180)}{preview.length > 180 ? '…' : ''}</summary>
          <pre>{body}</pre>
        </details>
      ) : (
        <p>{body}</p>
      )}
    </li>
  )
}

function EmptyWorkspace() {
  return (
    <div className="workspace-empty">
      <p>{EMPTY_WORKSPACE_PROMPT}</p>
    </div>
  )
}

interface ConversationTurnState {
  id: string
  user: TimelineEntry | null
  response: TimelineEntry[]
}

export function groupTimelineByTurn(timeline: TimelineEntry[]): ConversationTurnState[] {
  const turns: ConversationTurnState[] = []
  for (const item of timeline) {
    if (item.kind === 'user') {
      turns.push({ id: item.id, user: item, response: [] })
      continue
    }
    const current = turns.at(-1)
    if (current) current.response.push(item)
    else turns.push({ id: `prelude-${item.id}`, user: null, response: [item] })
  }
  return turns
}

export function ConversationTurn({
  turn,
  active,
  onApproval,
  onEdit,
  editDisabled,
  clarification,
  onAnswer,
  answerDisabled,
  clarificationAnswered,
  approvalDocked = false,
  files = [],
}: {
  turn: ConversationTurnState
  active: boolean
  onEdit?: (item: TimelineEntry, text: string) => Promise<void>
  editDisabled?: string
  clarification?: SessionSnapshot['pendingClarification']
  onAnswer?: (answer: string) => Promise<void>
  answerDisabled?: boolean
  clarificationAnswered?: boolean
  approvalDocked?: boolean
  files?: AssistantResource[]
  onApproval: (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => void
}) {
  const presentation = turnPresentation(turn.response, turn.user?.text, active, clarificationAnswered)
  const liveToolId = currentLiveToolId(presentation, active)
  const recordedPlan = turn.response.findLast((item) => item.kind === 'plan' && item.plan)?.plan
  const answerText = presentation.foreground.filter((item) => item.kind === 'assistant').map((item) => item.text).join('\n\n')
  return (
    <section className="conversation-turn" data-turn-id={turn.id}>
      {turn.user && <UserMessage item={turn.user} onEdit={onEdit} editDisabled={editDisabled} />}
      {(turn.response.length > 0 || active) && (
        <div className="assistant-turn" aria-label="Lumen 回答">
          <TurnSourcesProvider entries={turn.response} answer={answerText}>
          <div className="assistant-turn-body">
            {!approvalDocked && presentation.activity.some((item) => item.pendingApproval) && <div className="turn-attention" aria-label="需要确认的操作">
              {presentation.activity.filter((item) => item.pendingApproval).map((item) => <TimelineRow key={item.id} item={item} onApproval={onApproval} />)}
            </div>}
            {presentation.activity.length > 0 && (
              <TurnActivity
                active={active}
                elapsedSeconds={turn.user?.elapsedSeconds}
                presentation={presentation}
                onApproval={onApproval}
              />
            )}
            {presentation.foreground.filter((item) => !(clarification && item.status === 'waiting_for_user')).map((item) => (
              <TimelineRow key={item.id} item={item} onApproval={onApproval} />
            ))}
            {clarification && onAnswer && (
              <ClarificationPrompt key={clarification.id} question={clarification}
                onAnswer={onAnswer} disabled={answerDisabled} />
            )}
            {active && !presentation.requiresAttention && !liveToolId
              && presentation.activity.length === 0 && <LiveThinkingIndicator />}
            {!active && <DocumentResults entries={turn.response} resources={files} />}
            {!active && answerText && <footer className="answer-toolbar" aria-label="回答操作">
              <CopyButton text={answerText} />
              <ReportReadingButton content={answerText} entries={turn.response} />
            </footer>}
            {!active && <TurnSourcesSummary />}
            {!active && recordedPlan && <details className="completed-plan"><summary>查看本轮计划</summary>
              <PlanPanel plan={recordedPlan} />
            </details>}
          </div>
          </TurnSourcesProvider>
        </div>
      )}
    </section>
  )
}

function currentLiveToolId(presentation: TurnPresentation, active: boolean) {
  if (!active || presentation.requiresAttention) return null
  return presentation.activity.findLast((item) => item.kind === 'tool'
    && ['running', 'approved'].includes(item.status ?? ''))?.id ?? null
}

function LiveThinkingIndicator() {
  return (
    <div className="turn-live-placeholder" role="status" aria-label="Lumen 正在思考">
      <ThinkingOrb />
      <strong>正在思考</strong>
    </div>
  )
}

function TurnActivity({
  active,
  elapsedSeconds,
  presentation,
  onApproval,
}: {
  active: boolean
  elapsedSeconds?: number
  presentation: TurnPresentation
  onApproval: (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => void
}) {
  const pendingCalls = presentation.activity.filter((item) => item.pendingApproval).map((item) => item.id).join(',')
  const phase = pendingCalls ? `approval:${pendingCalls}` : active ? 'running' : presentation.terminalStatus ?? 'completed'
  const shouldExpand = active || Boolean(pendingCalls)
    || !['completed', 'waiting_for_user', 'clarification_answered'].includes(phase)
  const [manualExpanded, setManualExpanded] = useState<{ phase: string; expanded: boolean } | null>(null)
  const expanded = manualExpanded?.phase === phase ? manualExpanded.expanded : shouldExpand
  const contentId = useId()
  const summaryRef = useRef<HTMLButtonElement>(null)
  const contentRef = useRef<HTMLDivElement>(null)
  const previewFocused = useRef(false)
  const duration = !active ? activityDuration(elapsedSeconds) : null
  const reading = readableActivity(presentation.activity)
  const runningTools = active ? presentation.activity.filter(isRunningTool) : []
  const entries = reading.groups.flatMap((group) => group.entries).filter((item) => !item.pendingApproval)
  const latest = entries.at(-1)
  const liveThoughtId = active && !presentation.requiresAttention && runningTools.length === 0
    && !presentation.foreground.some((item) => item.kind === 'assistant') && latest?.kind === 'thinking'
    ? latest.id : null
  const latestProgress = active && !presentation.foreground.some((item) => item.kind === 'assistant')
    ? reading.groups.flatMap((group) => group.entries)
      .findLast((item) => ['commentary', 'progress'].includes(item.kind) && item.text.trim()) : undefined

  useEffect(() => {
    if ((!active || !expanded) && previewFocused.current) {
      summaryRef.current?.focus({ preventScroll: true })
      previewFocused.current = false
    }
    if (!expanded && contentRef.current?.contains(document.activeElement)) {
      summaryRef.current?.focus({ preventScroll: true })
    }
  }, [active, expanded])

  return (
    <section
      className={`turn-activity ${active ? 'is-active' : ''} ${presentation.requiresAttention ? 'needs-attention' : ''}`}
      aria-label="处理过程"
    >
      <button
        className="turn-activity-summary"
        type="button"
        ref={summaryRef}
        aria-expanded={expanded}
        aria-controls={contentId}
        title={`${activityMeta(presentation)}${duration ? '；耗时包括本轮模型、工具执行及等待' : ''}`}
        onClick={() => setManualExpanded({ phase, expanded: !expanded })}
      >
        <span className={`turn-activity-heading ${active && !presentation.requiresAttention ? 'is-live' : ''}`}>
          {active && !presentation.requiresAttention && (runningTools.length > 0
            ? <span className="tool-glyph" aria-hidden="true"><ToolGlyph item={runningTools[0]} /></span>
            : <ThinkingOrb />)}
          <strong>{!active && phase === 'completed' && duration ? `思考了 ${duration}` : activityTitle(presentation, active)}</strong>
        </span>
        {active && presentation.toolCount > 0 && <span className="turn-activity-count">{presentation.toolCount} 次工具调用</span>}
        {active && presentation.errorCount > 0 && <span className="turn-activity-errors">{presentation.errorCount} 次失败记录</span>}
        <CaretDown size={15} className={expanded ? 'is-expanded' : ''} aria-hidden="true" />
      </button>
      <span className="sr-only" role="status" aria-live="polite" aria-atomic="true">{active ? activityTitle(presentation, active) : ''}</span>
      {active && !expanded && (latestProgress || runningTools.length > 0) && <div className="turn-activity-preview" aria-label="当前进展"
        onFocusCapture={() => { previewFocused.current = true }}
        onBlurCapture={(event) => {
          if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) previewFocused.current = false
        }}>
        {latestProgress && <p className="turn-progress-preview">{latestProgress.text.replace(/\s+/g, ' ').trim()}</p>}
        {runningTools.length > 0 && <div className="turn-running-tools">
          {runningTools.map((item) => <TimelineRow key={item.id} item={item} onApproval={onApproval} process live={!presentation.requiresAttention} />)}
        </div>}
      </div>}
      <div className="turn-activity-list" id={contentId} ref={contentRef} hidden={!expanded}
        onFocusCapture={() => { previewFocused.current = true }}
        onBlurCapture={(event) => {
          if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) previewFocused.current = false
        }}>
        {reading.groups.map((group) => group.label ? (
          <details className="activity-tool-group" key={group.id}>
            <summary>
              <span className="tool-glyph" aria-hidden="true"><ToolGlyph item={group.entries[0]} /></span>
              <span>{group.label}</span>
              <CaretDown size={14} aria-hidden="true" />
            </summary>
            {group.entries.map((item) => <TimelineRow key={item.id} item={item} onApproval={onApproval} process />)}
          </details>
        ) : group.entries.filter((item) => !item.pendingApproval).map((item) => (
          <TimelineRow key={item.id} item={item} onApproval={onApproval} process
            live={active && !presentation.requiresAttention && (isRunningTool(item) || item.id === liveThoughtId)} />
        )))}
        {reading.diagnostics.length > 0 && <details className="process-diagnostics">
          <summary>运行事件 <span>{reading.diagnostics.length}</span></summary>
          <p className="process-diagnostics-note">上下文、工作对象及后台事件；完整记录可在运行记录中查看。</p>
          {reading.diagnostics.map((item) => <TimelineRow key={item.id} item={item} onApproval={onApproval} />)}
        </details>}
      </div>
    </section>
  )
}

function TimelineRow({
  item,
  onApproval,
  process = false,
  live = false,
}: {
  item: TimelineEntry
  process?: boolean
  live?: boolean
  onApproval: (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => void
}) {
  const [expanded, setExpanded] = useState(false)
  const detailId = useId()

  if (item.kind === 'plan') return null
  if (process && item.kind === 'thinking') {
    return <article className={`process-thought ${live ? 'is-live' : ''}`}>
      <div className="process-step-heading"><Sparkle size={16} aria-hidden="true" />
        <span className={live ? 'process-live-text' : ''}>{live ? '正在思考' : '思考过程'}</span>
      </div>
      <div className="process-thought-content"><MarkdownMessage content={item.text} /></div>
    </article>
  }
  if (item.kind === 'assistant') {
    return (
      <article className="timeline-assistant">
        <div className="assistant-content">
          <MarkdownMessage content={item.text} />
        </div>
      </article>
    )
  }
  if (item.kind === 'tool') {
    const sourceLabel = toolSourceLabel(item)
    const failed = item.isError || ['denied', 'error', 'failed'].includes(item.status ?? '')
    const compact = ['completed', 'ok', 'success'].includes(item.status ?? '')
      && !item.isError
      && !item.pendingApproval
    return (
      <article className={`web-tool-card ${compact ? 'is-compact' : ''} ${live ? 'is-live' : ''} ${failed ? 'is-error' : ''} ${item.pendingApproval ? 'is-pending' : ''} ${expanded ? 'is-expanded' : ''}`}>
        <button
          className="tool-summary"
          type="button"
          aria-expanded={expanded}
          aria-controls={detailId}
          aria-label={`${process ? toolActivityLabel(item) : String(item.callView?.title ?? item.toolName ?? '')}，${toolStatus(item)}`}
          title={process ? toolActivityLabel(item) : undefined}
          onClick={() => setExpanded((value) => !value)}
        >
          <span className="tool-glyph" aria-hidden="true"><ToolGlyph item={item} /></span>
          <span>
            <span className="tool-title-line">
              <strong className={live ? 'process-live-text' : undefined}>{process ? toolActivityLabel(item) : String(item.callView?.title ?? item.toolName ?? '')}</strong>
              {sourceLabel && !process && <b>{sourceLabel}</b>}
            </span>
            {!process && <small>{String(item.callView?.detail ?? toolTarget(item))}</small>}
          </span>
          {(!process || item.pendingApproval || failed) && <em className={`is-${item.status ?? 'idle'}`}>{toolStatus(item)}</em>}
          <CaretDown size={14} className={expanded ? 'is-expanded' : ''} aria-hidden="true" />
        </button>
        <div id={detailId} hidden={!expanded && !item.pendingApproval}>
          {(item.presentation || item.pendingApproval) && (expanded || item.pendingApproval) && (
            <div className="approval-request">
              <strong>{item.presentation?.title ?? item.toolName}</strong>
              <pre>{item.presentation ? expanded ? item.presentation.full_text : item.presentation.preview : JSON.stringify(item.args, null, 2)}</pre>
              {item.pendingApproval && <p className="approval-scope-note">{item.risk === 'confirm'
                ? '此敏感操作必须每次确认，不能记住授权。'
                : item.toolName === 'run_command'
                  ? '记住授权仅适用于完全相同的命令、参数与目录；不同命令仍需确认。'
                  : '记住授权适用于同一来源的此工具；会话授权限当前会话，项目授权跨会话生效。'}</p>}
              {item.pendingApproval && item.callId && (
                <div>
                  <button type="button" onClick={() => onApproval(item.callId!, true, 'once')}>允许一次</button>
                  {item.risk !== 'confirm' && <>
                    <button type="button" onClick={() => onApproval(item.callId!, true, 'session')}>本会话始终允许</button>
                    <button type="button" onClick={() => onApproval(item.callId!, true, 'always')}>本项目始终允许</button>
                  </>}
                  <button type="button" className="deny" onClick={() => onApproval(item.callId!, false)}>拒绝</button>
                </div>
              )}
            </div>
          )}
          {expanded && <ToolOutputDetails item={item} />}
        </div>
        {!expanded && (!process || failed) && (item.resultView?.preview || item.preview) && (
          <p>{String(item.resultView?.preview ?? item.preview ?? '')}</p>
        )}
      </article>
    )
  }
  const isProcessText = process && ['progress', 'thinking', 'commentary'].includes(item.kind)
  const label = isProcessText ? '' : timelineNoteLabel(item.kind)
  return (
    <article className={`timeline-note is-${item.kind} ${isProcessText ? 'process-text' : ''} ${live ? 'is-live' : ''}`}>
      <div>
        {label && <strong>{label}</strong>}
        {item.kind === 'progress' || item.kind === 'thinking' || item.kind === 'commentary'
          ? <MarkdownMessage content={item.text} />
          : <p>{item.status === 'cancelled' && ['cancelled', 'run cancelled'].includes(item.text.trim().toLowerCase())
            ? '已停止生成。' : item.text}</p>}
      </div>
    </article>
  )
}

function ToolGlyph({ item }: { item: TimelineEntry }) {
  const family = String(item.callView?.family ?? '').toLowerCase()
  if (item.toolName === 'view_image' && ['', 'other', 'read'].includes(family)) return <Images size={18} />
  if (item.toolName === 'search_tools' && ['', 'other'].includes(family)) return <Wrench size={18} />
  if (family === 'read') return <BookOpen size={18} />
  if (family === 'list') return <FolderOpen size={18} />
  if (family === 'search') return <MagnifyingGlass size={18} />
  if (family === 'edit') return <PencilSimpleLine size={18} />
  if (family === 'command') return <TerminalWindow size={18} />
  if (family === 'plan') return <ListChecks size={18} />
  if (family === 'mcp') return <PlugsConnected size={18} />
  if (family === 'skill') return <Sparkle size={18} />
  if (family === 'web') return <Globe size={18} />
  if (family) return <Wrench size={18} />
  const normalized = (item.toolName ?? '').toLowerCase()
  if (normalized.includes('search') || normalized.includes('find')) return <MagnifyingGlass size={18} />
  if (normalized.includes('edit') || normalized.includes('write') || normalized.includes('patch')) return <PencilSimpleLine size={18} />
  if (normalized.includes('list') || normalized.includes('directory')) return <FolderOpen size={18} />
  if (normalized.includes('read') || normalized.includes('file')) return <BookOpen size={18} />
  if (normalized.includes('command') || normalized.includes('exec')) return <TerminalWindow size={18} />
  return <Wrench size={18} />
}

function toolStatus(item: TimelineEntry) {
  if (item.status === 'cancelled') return '已取消'
  if (item.pendingApproval) return '等待审批'
  if (item.status === 'denied') return '已拒绝'
  if (item.isError || item.status === 'error' || item.status === 'failed') return '失败'
  if (item.status === 'running') return '运行中'
  if (item.status === 'approved') return '已允许'
  if (item.status === 'completed' || item.status === 'ok' || item.status === 'success') {
    return item.isError ? '失败' : '完成'
  }
  return item.status ?? ''
}

function queueModeLabel(mode: string) {
  return mode === 'follow_up' ? '完成后继续' : '立即补充'
}

function toolTarget(item: TimelineEntry) {
  const args = item.args ?? {}
  const candidate = args.path ?? args.cwd ?? args.query
  if (typeof candidate === 'string') return candidate
  const argv = args.argv
  if (Array.isArray(argv)) return argv.join(' ')
  return item.status ?? ''
}

// Web 端支持的 slash 命令清单，按 TUI /help 的分组风格排列；
// 只列 Web 已实现的子集（TUI 注册表见 src/lumen/ui/slash_commands.py）。
const HELP_TEXT = `可用命令：

Session:
  /new                              — 新建任务
  /retry                            — 重试上次任务
  /clear                            — 清空当前显示（保留会话上下文）
  /checkpoints                      — 查看 checkpoint 并创建 Session 分支
  /dequeue                          — 撤回尚未执行的排队输入
Runtime:
  /agents                           — 查看和协调子 Agent
  /transcript                       — 搜索结构化 transcript
Model:
  /model [name]                     — 打开模型选择器或按名称切换
  /mode [manual|accept_edits|auto]  — 打开审批选择器或按名称切换
  /tasks                            — 查看运行记录中的计划历史
  /plan [task]                      — 切换规划模式，或为指定任务生成计划
Context:
  /context                          — 查看上下文预算
  /context sources                  — 查看当前会话上下文来源
  /instructions                     — 查看 prompt 模式、动态上下文与来源
  /compact [focus]                  — 压缩上下文
  /clarification cancel             — 取消待回答澄清
MCP:
  /mcp                              — 查看 MCP 连接
  /prompts                          — 查看 MCP prompt 模板
  /prompt <server:name> [key=value] — 渲染并运行 MCP prompt 模板
  /resource [refresh|unload] <ref>  — 激活 / 刷新 / 卸载 MCP resource
Memory:
  /memory [list|remember|edit|forget|use|learn|incognito] — 管理记忆
Other:
  /tools                            — 查看可用工具
  /hooks                            — 查看 hooks 与触发统计
  /copy                             — 复制最新助手回复
  /skills                           — 查看可用技能
  /skill:<name> [args]              — 手动触发技能
  /skill unload <name>              — 卸载当前会话技能
  /help                             — 显示本帮助

输入 / 打开命令菜单，继续输入可筛选。`

function formatControlResult(result: {
  message: string
  payload?: Record<string, unknown>
}) {
  if (!result.payload || Object.keys(result.payload).length === 0) return result.message
  return `${result.message}\n\n${JSON.stringify(result.payload, null, 2)}`
}

function usageLabel(usage: Record<string, unknown> | null) {
  if (!usage) return ''
  const input = usage.input_tokens ?? usage.inputTokens
  const output = usage.output_tokens ?? usage.outputTokens
  if (typeof input !== 'number' && typeof output !== 'number') return ''
  return ` · ${Number(input ?? 0) + Number(output ?? 0)} tokens`
}
