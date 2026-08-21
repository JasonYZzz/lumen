'use client'

import {
  Archive,
  ArrowDown,
  ArrowCounterClockwise,
  ArrowsClockwise,
  CaretDown,
  Check,
  CheckCircle,
  Circle,
  CircleNotch,
  ClockCounterClockwise,
  Copy,
  DotsThree,
  FileCode,
  FolderSimple,
  FolderOpen,
  GearSix,
  HardDrives,
  Info,
  List,
  ListMagnifyingGlass,
  MagnifyingGlass,
  NotePencil,
  PencilSimpleLine,
  PlugsConnected,
  Plus,
  Robot,
  ShieldCheck,
  Sparkle,
  TerminalWindow,
  TreeStructure,
  Trash,
  WarningCircle,
  X,
} from '@phosphor-icons/react'
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { exchangeLaunchToken, lumenApi, subscribeRun } from '@/lib/api/client'
import type {
  AgentRecord,
  ApprovalMode,
  Bootstrap,
  CapabilityInventory,
  CheckpointRecord,
  ConfigurationSnapshot,
  ConfiguredModel,
  EventEnvelope,
  QueueMode,
  SessionSummary,
  TimelineEntry,
} from '@/lib/api/types'
import { initialRunState, runReducer } from '@/lib/state/run-reducer'
import {
  baseSlashCommands,
  parsePromptInvocation,
  type SlashCommand,
} from '@/lib/slash-commands'
import { Composer } from './composer'
import { ChoiceMenu, type ChoiceOption } from './choice-menu'
import { LandingEntry } from './landing-entry'
import { LiveVoiceControls } from './live-voice-controls'
import { MarkdownMessage } from './markdown-message'
import { SessionActionsMenu } from './session-actions-menu'
import { EMPTY_WORKSPACE_PROMPT } from '@/lib/copy'
import {
  activityMeta,
  activityTitle,
  transcriptEventLabel,
  timelineNoteLabel,
  toolSourceLabel,
  turnPresentation,
  type TurnPresentation,
} from '@/lib/turn-activity'
import { useModalFocus } from './use-modal-focus'

function requestId() {
  return crypto.randomUUID()
}

const approvalChoices: Array<ChoiceOption<ApprovalMode>> = [
  { value: 'manual', label: '每次确认', description: '工具执行前逐项确认，适合审慎操作。' },
  { value: 'accept_edits', label: '自动改文件', description: '文件修改自动允许，其他动作仍按规则确认。' },
  { value: 'auto', label: '自动执行', description: '在权限与 Sandbox 约束内持续执行。' },
]

export function LumenApp() {
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
  const [queueMode, setQueueMode] = useState<QueueMode>('steer')
  const [loading, setLoading] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [confirmAuto, setConfirmAuto] = useState(false)
  const [planFeedback, setPlanFeedback] = useState('')
  const [planReviewBusy, setPlanReviewBusy] = useState(false)
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)
  const [inspectorOpen, setInspectorOpen] = useState(false)
  const [checkpointsOpen, setCheckpointsOpen] = useState(false)
  const [transcriptOpen, setTranscriptOpen] = useState(false)
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [checkpoints, setCheckpoints] = useState<CheckpointRecord[]>([])
  const [controlBusy, setControlBusy] = useState(false)
  const closeStreamRef = useRef<(() => void) | null>(null)
  const timelineRef = useRef<HTMLElement>(null)
  const stickToLatestRef = useRef(true)

  const refreshChrome = useCallback(async () => {
    const [nextBootstrap, nextSessions] = await Promise.all([
      lumenApi.bootstrap(),
      lumenApi.listSessions(true),
    ])
    setBootstrap(nextBootstrap)
    setSessions(nextSessions)
  }, [])

  const attachRun = useCallback(
    (runId: string) => {
      closeStreamRef.current?.()
      closeStreamRef.current = subscribeRun(
        runId,
        (event: EventEnvelope) => {
          dispatch({ type: 'event', event })
          if (
            event.type === 'run.completed' ||
            event.type === 'run.waiting_for_user' ||
            event.type === 'run.failed' ||
            event.type === 'run.cancelled'
          ) {
            closeStreamRef.current = null
            void refreshChrome()
          }
        },
        (message) => dispatch({ type: 'local-error', message }),
      )
    },
    [refreshChrome],
  )

  const openSession = useCallback(
    async (id: string) => {
      setSessionMenu(null)
      closeStreamRef.current?.()
      closeStreamRef.current = null
      stickToLatestRef.current = true
      setShowJumpToLatest(false)
      setLoading(true)
      try {
        const snapshot = await lumenApi.session(id)
        setSessionId(id)
        setBootstrap((current) => current ? {
          ...current,
          approvalMode: snapshot.approvalMode,
          collaborationMode: snapshot.collaborationMode,
        } : current)
        dispatch({ type: 'snapshot', snapshot })
        const url = new URL(window.location.href)
        url.searchParams.set('session', id)
        window.history.replaceState({}, '', `${url.pathname}${url.search}`)
        if (snapshot.activeRunId) attachRun(snapshot.activeRunId)
      } catch (error) {
        dispatch({
          type: 'local-error',
          message: error instanceof Error ? error.message : '无法恢复会话',
        })
      } finally {
        setLoading(false)
        setSidebarOpen(false)
      }
    },
    [attachRun],
  )

  useEffect(() => {
    let disposed = false
    const initialise = async () => {
      try {
        await exchangeLaunchToken()
        const [nextBootstrap, nextSessions] = await Promise.all([
          lumenApi.bootstrap(),
          lumenApi.listSessions(true),
        ])
        if (disposed) return
        setBootstrap(nextBootstrap)
        setSessions(nextSessions)
        const requested = new URL(window.location.href).searchParams.get('session')
        if (requested) await openSession(requested)
      } catch (error) {
        if (!disposed) {
          dispatch({
            type: 'local-error',
            message: error instanceof Error ? error.message : '无法连接 Lumen Web',
          })
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
  }, [openSession])

  const createSession = useCallback(async () => {
    const created = await lumenApi.createSession()
    await refreshChrome()
    await openSession(created.sessionId)
    return created.sessionId
  }, [openSession, refreshChrome])

  const refreshSessionCapabilities = useCallback(async () => {
    if (!sessionId) return
    const [snapshot, agents] = await Promise.all([
      lumenApi.session(sessionId),
      lumenApi.listAgents(sessionId),
    ])
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

  const waiveEffect = useCallback(async (effect: Record<string, unknown>) => {
    // TODO(设计审计 W6)：window.prompt 同上，后续替换为自绘弹层
    if (!sessionId) return
    const id = String(effect.id ?? '')
    const reason = window.prompt('说明为什么可以跳过该验证')
    if (!id || !reason?.trim()) return
    await lumenApi.waiveVerification(sessionId, [id], reason.trim())
    await refreshSessionCapabilities()
  }, [refreshSessionCapabilities, sessionId])

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
        if (!argument) {
          dispatch({ type: 'local-message', message: '在 `/plan` 后输入任务，Lumen 会先生成计划供你确认。' })
          setInput('/plan ')
          return true
        }
        if (run.runId) {
          dispatch({ type: 'local-error', message: '/plan 在任务运行期间不可用，请先停止当前任务。' })
          return true
        }
        const targetSession = sessionId ?? (await createSession())
        await lumenApi.updateSessionSettings(targetSession, { collaborationMode: 'plan' })
        setBootstrap((current) => current ? { ...current, collaborationMode: 'plan' } : current)
        const started = await lumenApi.startRun(targetSession, argument, requestId())
        dispatch({ type: 'run-registered', runId: started.runId })
        attachRun(started.runId)
        await refreshChrome()
        setInput('')
        return true
      }
      if (name === '/mode') {
        if (!argument) {
          dispatch({
            type: 'local-message',
            message: `审批：${bootstrap?.approvalMode ?? 'manual'}\n\n可用：\`manual\` · \`accept_edits\` · \`auto\`；需要先规划时使用 \`/plan 任务\`。`,
          })
          setInput('')
          return true
        }
        if (!sessionId) {
          dispatch({ type: 'local-error', message: '请先创建或打开任务。' })
        } else if (argument === 'auto') {
          setConfirmAuto(true)
        } else if (argument === 'plan') {
          await lumenApi.updateSessionSettings(sessionId, { collaborationMode: 'plan' })
          setBootstrap((current) => current ? { ...current, collaborationMode: 'plan' } : current)
        } else if (argument === 'manual' || argument === 'accept_edits') {
          await lumenApi.updateSessionSettings(sessionId, {
            approvalMode: argument,
            collaborationMode: 'default',
          })
          setBootstrap((current) => current ? {
            ...current,
            approvalMode: argument,
            collaborationMode: 'default',
          } : current)
        } else {
          dispatch({ type: 'local-error', message: `不支持的模式：${argument}` })
        }
        setInput('')
        return true
      }
      if (name === '/model') {
        if (!argument) {
          dispatch({
            type: 'local-message',
            message: `当前模型：${bootstrap?.activeModel ?? '未知'}\n\n${bootstrap?.availableModels.map((model) => `\`${model}\``).join(' · ') || '暂无可用模型'}`,
          })
        } else {
          await lumenApi.updateWorkspaceSettings({ model: argument })
          await refreshChrome()
        }
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
        const latest = run.timeline.filter((item) => item.kind === 'assistant').at(-1)
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
        await dequeueInputs()
        setInput('')
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
      run.timeline,
      sessionId,
    ],
  )

  const submit = useCallback(async () => {
    const prompt = input.trim()
    if (!prompt) return
    try {
      if (prompt.startsWith('/') && (await handleSlashCommand(prompt))) return
      if (run.runId) {
        await lumenApi.queueInput(run.runId, prompt, queueMode)
        setInput('')
        return
      }
      const targetSession = sessionId ?? (await createSession())
      const started = await lumenApi.startRun(targetSession, prompt, requestId())
      dispatch({ type: 'run-registered', runId: started.runId })
      attachRun(started.runId)
      await refreshChrome()
      setInput('')
    } catch (error) {
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : '无法启动运行',
      })
    }
  }, [attachRun, createSession, handleSlashCommand, input, queueMode, refreshChrome, run.runId, sessionId])

  const stop = useCallback(async () => {
    if (!run.runId) return
    try {
      await lumenApi.cancelRun(run.runId)
    } catch (error) {
      dispatch({ type: 'local-error', message: error instanceof Error ? error.message : '取消失败' })
    }
  }, [run.runId])

  const setApprovalMode = useCallback(
    async (mode: ApprovalMode) => {
      if (!sessionId) return
      if (mode === 'auto') {
        setConfirmAuto(true)
        return
      }
      await lumenApi.updateSessionSettings(sessionId, { approvalMode: mode })
      setBootstrap((current) => current ? { ...current, approvalMode: mode } : current)
    },
    [sessionId],
  )

  const approveAuto = useCallback(async () => {
    if (!sessionId) return
    await lumenApi.updateSessionSettings(sessionId, { approvalMode: 'auto' })
    setBootstrap((current) => current ? { ...current, approvalMode: 'auto' } : current)
    setConfirmAuto(false)
    await refreshChrome()
  }, [sessionId])

  const reviewPlan = useCallback(async (action: 'approve' | 'reject') => {
    if (!sessionId || !run.planReviewRevision || planReviewBusy) return
    const feedback = planFeedback.trim()
    if (action === 'reject' && !feedback) {
      dispatch({ type: 'local-error', message: '驳回计划时需要填写反馈。' })
      return
    }
    setPlanReviewBusy(true)
    try {
      const started = await lumenApi.reviewPlan(
        sessionId,
        action,
        run.planReviewRevision,
        requestId(),
        feedback,
      )
      dispatch({ type: 'run-registered', runId: started.runId })
      setBootstrap((current) => current ? {
        ...current,
        collaborationMode: action === 'approve' ? 'default' : 'plan',
      } : current)
      setPlanFeedback('')
      attachRun(started.runId)
    } catch (error) {
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : '计划审批失败',
      })
    } finally {
      setPlanReviewBusy(false)
    }
  }, [attachRun, planFeedback, planReviewBusy, run.planReviewRevision, sessionId])

  const leaveSession = useCallback(() => {
    closeStreamRef.current?.()
    closeStreamRef.current = null
    setSessionId(null)
    setSessionMenu(null)
    dispatch({ type: 'reset' })
    const url = new URL(window.location.href)
    url.searchParams.delete('session')
    window.history.replaceState({}, '', `${url.pathname}${url.search}`)
  }, [])

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
  }, [run.timeline, run.queuedInputs.length, scrollToLatest])

  const activeSession = sessions.find((item) => item.sessionId === sessionId)
  const sessionIsArchived = Boolean(activeSession?.archived)
  const visibleSessions = sessions.filter((item) => item.archived === (sessionView === 'archived'))
  const workspaceBusy = Boolean(bootstrap?.activeRunId || run.runId)
  const isLanding = !sessionIsArchived
    && !loading
    && run.timeline.length === 0
    && !run.runId
    && run.queuedInputs.length === 0
  const statusLabel = run.runId
    ? '运行中'
    : workspaceBusy
      ? '其他会话运行中'
      : run.status === 'failed'
        ? '运行失败'
        : run.status === 'cancelled'
          ? '已停止'
          : run.status === 'completed'
            ? '已完成'
            : run.status === 'waiting_for_user'
              ? '等待回答'
            : '就绪'
  const slashCommands = useMemo<SlashCommand[]>(() => {
    const modelCommands = (bootstrap?.availableModels ?? []).map((model) => ({
      value: `/model ${model}`,
      description: model === bootstrap?.activeModel ? '当前模型' : '切换模型',
      keywords: 'model',
      kind: 'model' as const,
    }))
    const modeCommands: SlashCommand[] = [
      { value: '/mode manual', description: '每次确认', keywords: 'approval', kind: 'mode' },
      { value: '/mode accept_edits', description: '自动接受文件修改', keywords: 'approval', kind: 'mode' },
      { value: '/mode auto', description: '自动执行', keywords: 'approval', kind: 'mode' },
    ]
    const skillCommands = (bootstrap?.skills ?? []).map((skill) => ({
      value: `/skill:${skill.name} `,
      description: skill.description || '运行技能',
      keywords: 'skill',
      kind: 'skill' as const,
    }))
    return [...baseSlashCommands, ...modelCommands, ...modeCommands, ...skillCommands]
  }, [bootstrap])
  const composerSettings = (
    <div className="composer-settings">
      <ChoiceMenu
        className="is-model"
        label="模型"
        value={bootstrap?.activeModel ?? ''}
        disabled={!bootstrap || workspaceBusy}
        icon={<Sparkle size={14} />}
        options={(bootstrap?.availableModels ?? []).map((model) => ({
          value: model,
          label: model,
          description: model === bootstrap?.activeModel ? '当前使用的模型' : '切换后用于下一轮任务',
        }))}
        onChange={(model) => void lumenApi.updateWorkspaceSettings({ model }).then(refreshChrome)}
      />
      <ChoiceMenu
        label="审批模式"
        value={bootstrap?.approvalMode ?? 'manual'}
        icon={<ShieldCheck size={14} />}
        options={approvalChoices}
        onChange={(mode) => void setApprovalMode(mode)}
      />
    </div>
  )

  return (
    <main className="workspace-shell">
      <aside className={`session-sidebar ${sidebarOpen ? 'is-open' : ''}`}>
        <div className="sidebar-brand-row">
          <button className="brand" type="button" onClick={leaveSession}>lumen</button>
          <button className="sidebar-close" type="button" aria-label="关闭" onClick={() => setSidebarOpen(false)}>
            <X size={18} aria-hidden="true" />
          </button>
        </div>
        <button className="new-session" type="button" onClick={leaveSession}>
          <NotePencil size={18} aria-hidden="true" /> 新建任务
        </button>
        <div className="sidebar-section-heading">工作区</div>
        <div className="sidebar-workspace">
          <FolderSimple size={16} aria-hidden="true" />
          <span title={bootstrap?.workspace}>{bootstrap?.workspace.split('/').at(-1) ?? 'workspace'}</span>
        </div>
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
              最近任务
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
                    onClick={(event) => setSessionMenu((current) => (
                      current?.session.sessionId === session.sessionId
                        ? null
                        : { session, anchor: event.currentTarget }
                    ))}
                  >
                    <DotsThree size={18} weight="bold" aria-hidden="true" />
                  </button>
                </div>
              </div>
            ))
          )}
        </nav>
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

      <section className="agent-workspace">
        <header className={`workspace-header ${isLanding ? 'is-landing' : ''}`}>
          <div className="workspace-heading">
            <button className="sidebar-toggle" type="button" aria-label="打开任务列表" onClick={() => setSidebarOpen(true)}>
              <List size={19} aria-hidden="true" />
            </button>
            <div>
              <h1>{activeSession?.title ?? '新任务'}</h1>
              {(workspaceBusy || run.status === 'failed' || run.status === 'cancelled') && <p>
                <span className={workspaceBusy ? 'status-dot is-busy' : 'status-dot'} />
                {statusLabel}{usageLabel(run.usage)}
              </p>}
            </div>
          </div>
          <div className="workspace-controls" aria-label="工作区工具">
            {sessionId && <>
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
            </>}
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

        {settingsOpen && (
          <SettingsDialog
            workspace={bootstrap?.workspace ?? ''}
            runActive={Boolean(run.runId)}
            onClose={() => setSettingsOpen(false)}
          />
        )}

        {inspectorOpen && (
          <RuntimeInspector
            agents={run.agents}
            workProducts={run.workProducts}
            pendingEffects={run.pendingEffects}
            recoverableEffects={run.recoverableEffects}
            busy={controlBusy}
            onClose={() => setInspectorOpen(false)}
            onRefresh={() => void refreshSessionCapabilities()}
            onAgentAction={(agent, action) => void runAgentAction(agent, action)}
            onWaive={(effect) => void waiveEffect(effect)}
          />
        )}

        {checkpointsOpen && (
          <CheckpointInspector
            checkpoints={checkpoints}
            busy={controlBusy}
            onClose={() => setCheckpointsOpen(false)}
            onFork={(checkpoint) => void forkCheckpoint(checkpoint)}
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
          <div className="auto-confirm" role="alert">
            <ShieldCheck size={19} />
            <div><strong>开启 auto 模式？</strong><span>已分类的写入、执行和外部工具将不再逐次确认。</span></div>
            <button type="button" onClick={() => void approveAuto()}>确认开启</button>
            <button type="button" className="quiet" onClick={() => setConfirmAuto(false)}>取消</button>
          </div>
        )}

        {(run.planReviewStatus === 'review_pending'
          || run.planReviewStatus === 'approved_waiting_to_execute')
          && run.planReviewRevision && (
          <div className="plan-review" role="alert">
            <div>
              <strong>计划 revision {run.planReviewRevision} 等待审批</strong>
              <span>批准后将以当前审批模式开始新的执行 turn。</span>
            </div>
            <textarea
              value={planFeedback}
              disabled={planReviewBusy}
              placeholder="如需驳回，请填写修改意见"
              aria-label="计划驳回反馈"
              onChange={(event) => setPlanFeedback(event.target.value)}
            />
            <button type="button" disabled={planReviewBusy} onClick={() => void reviewPlan('approve')}>
              {run.planReviewStatus === 'approved_waiting_to_execute' ? '继续执行' : '批准并执行'}
            </button>
            <button
              type="button"
              className="quiet"
              disabled={planReviewBusy || !planFeedback.trim()}
              onClick={() => void reviewPlan('reject')}
            >
              驳回并重新规划
            </button>
          </div>
        )}

        <div className={`conversation-stage ${isLanding ? 'is-landing' : ''}`}>
          {isLanding ? (
            <LandingEntry
              value={input}
              busy={false}
              queueMode={queueMode}
              slashCommands={slashCommands}
              onChange={setInput}
              onQueueModeChange={setQueueMode}
              onSubmit={() => void submit()}
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
                      active={Boolean(run.runId) && index === turns.length - 1}
                      onApproval={(callId, approved, scope) => {
                        if (run.runId) {
                          void lumenApi.decideApproval(run.runId, callId, approved, scope)
                        }
                      }}
                    />
                  ))
                )}
                <div className="timeline-end" aria-hidden="true" />
              </section>

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
                {run.runId && (
                  <div className="run-live-status" role="status">
                    <CircleNotch size={14} className="spin" />
                    <span><strong>正在处理</strong></span>
                  </div>
                )}
                {run.queuedInputs.length > 0 && (
                  <div className="queued-inputs">
                    {run.queuedInputs.map((item) => <span key={item.id}>{queueModeLabel(item.mode)} · {item.text}</span>)}
                    <button type="button" onClick={() => void dequeueInputs()}>撤回到输入框</button>
                  </div>
                )}
                <Composer
                  value={input}
                  busy={Boolean(run.runId)}
                  queueMode={queueMode}
                  slashCommands={slashCommands}
                  onChange={setInput}
                  onQueueModeChange={setQueueMode}
                  onSubmit={() => void submit()}
                  onStop={() => void stop()}
                  settingsControl={composerSettings}
                  liveControl={(
                    <LiveVoiceControls
                      enabled={Boolean(bootstrap?.liveEnabled)}
                      blocked={Boolean(run.runId)}
                      ensureSession={async () => sessionId ?? createSession()}
                      onEnded={(id) => void openSession(id)}
                    />
                  )}
                />
                </footer>
              )}
            </>
          )}
        </div>
      </section>
    </main>
  )
}

type SettingsSection = 'overview' | 'models' | 'extensions' | 'agents'

interface ModelDraft {
  originalName: string | null
  name: string
  id: string
  api: Exclude<ConfiguredModel['api'], null> | ''
  baseUrl: string
  apiKeyEnv: string
  setDefault: boolean
  settings: Record<string, unknown>
  context: Record<string, unknown>
  authKind: ConfiguredModel['authKind']
}

function modelDraft(model: ConfiguredModel): ModelDraft {
  return {
    originalName: model.name,
    name: model.name,
    id: model.id,
    api: model.api ?? '',
    baseUrl: model.baseUrl ?? '',
    apiKeyEnv: model.apiKeyEnv ?? '',
    setDefault: model.isDefault,
    settings: { ...model.settings },
    context: { ...model.context },
    authKind: model.authKind,
  }
}

function emptyModelDraft(): ModelDraft {
  return {
    originalName: null,
    name: '',
    id: '',
    api: '',
    baseUrl: '',
    apiKeyEnv: '',
    setDefault: false,
    settings: {},
    context: {},
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
      || Object.keys(draft.settings).some((key) => draft.settings[key] !== undefined)
      || Object.keys(draft.context).some((key) => draft.context[key] !== undefined),
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
  )
}

function SettingsDialog({
  workspace,
  runActive,
  onClose,
}: {
  workspace: string
  runActive: boolean
  onClose: () => void
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
        context: draft.context,
        setDefault: draft.setDefault,
      })
      setConfiguration(result)
      const saved = result.models.find((model) => model.name === draft.name.trim())
      if (saved) setDraft(modelDraft(saved))
      setRestartRequired(Boolean(result.restartRequired))
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
      setDeleteArmed(false)
      setCloseArmed(false)
    } catch (removeError) {
      setError(removeError instanceof Error ? removeError.message : '删除模型失败')
    } finally {
      setBusy(false)
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
    if (draftChanged && !closeArmed) {
      setCloseArmed(true)
      return
    }
    onClose()
  }, [busy, closeArmed, draftChanged, onClose])
  const dialogRef = useModalFocus(requestClose)

  const navItems: Array<{
    id: SettingsSection
    label: string
    icon: typeof GearSix
    count?: number
  }> = [
    { id: 'overview', label: '通用设置', icon: GearSix },
    { id: 'models', label: '模型', icon: HardDrives, count: configuration?.models.length },
    {
      id: 'extensions',
      label: '扩展能力',
      icon: PlugsConnected,
      count: (capabilities?.skills.length ?? 0) + (capabilities?.mcp_servers.length ?? 0),
    },
    { id: 'agents', label: 'Agent 预设', icon: Robot, count: capabilities?.agent_profiles.length },
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
        <header className="settings-header">
          <div>
            <strong id="settings-title">设置</strong>
            <span>{workspace || 'Lumen 工作区'}</span>
          </div>
          <div>
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
            <button type="button" className="settings-close" aria-label="关闭设置" onClick={requestClose}>
              <X size={17} aria-hidden="true" />
            </button>
          </div>
        </header>
        <div className="settings-layout">
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
                  <Icon size={16} aria-hidden="true" />
                  <span>{item.label}</span>
                  {item.count !== undefined && <small>{item.count}</small>}
                </button>
              )
            })}
          </nav>
          <div className="settings-content">
            {loading ? (
              <div className="settings-loading"><CircleNotch size={18} className="spin" /> 正在读取配置</div>
            ) : error && !configuration ? (
              <div className="settings-error" role="alert">
                <WarningCircle size={18} />
                <span>{error}</span>
                <button type="button" onClick={() => void load()}>重试</button>
              </div>
            ) : section === 'overview' ? (
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
                    disabled={!editable || busy}
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
                {runActive && (
                  <div className="settings-notice"><Info size={17} /><span>任务运行期间配置为只读，请在运行结束后保存。</span></div>
                )}
                {!configuration?.editable && (
                  <div className="settings-notice"><Info size={17} /><span>{configuration?.editReason}</span></div>
                )}
                {closeArmed && draftChanged && (
                  <div className="settings-notice is-warning" role="alert">
                    <WarningCircle size={17} />
                    <span><strong>有尚未保存的更改</strong>关闭设置会放弃当前模型表单中的修改。</span>
                    <div>
                      <button type="button" onClick={() => setCloseArmed(false)}>继续编辑</button>
                      <button type="button" className="is-danger" onClick={onClose}>放弃更改</button>
                    </div>
                  </div>
                )}
                <div className="model-settings-body">
                  <div className="model-list" role="listbox" aria-label="已配置模型">
                    {configuration?.models.map((model) => (
                      <button
                        type="button"
                        role="option"
                        aria-selected={draft?.originalName === model.name}
                        className={draft?.originalName === model.name ? 'is-active' : ''}
                        key={model.name}
                        disabled={busy || (draftChanged && draft?.originalName !== model.name)}
                        title={draftChanged && draft?.originalName !== model.name ? '请先保存或还原当前更改' : undefined}
                        onClick={() => {
                          setDraft(modelDraft(model))
                          setDeleteArmed(false)
                          setCloseArmed(false)
                        }}
                      >
                        <span><strong>{model.name}</strong><small>{model.id}</small></span>
                        {model.isDefault && <em>默认</em>}
                      </button>
                    ))}
                  </div>
                  {draft && (
                    <form className="model-form" onSubmit={(event) => { event.preventDefault(); void save() }}>
                      {error && <div className="settings-inline-error" role="alert"><span>{error}</span><button type="button" onClick={() => void load()}>重新读取</button></div>}
                      <div className="model-form-grid">
                        <label><span>配置名称</span><input value={draft.name} disabled={draft.originalName !== null || busy || !editable} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如 local-qwen" /></label>
                        <label><span>模型 ID</span><input value={draft.id} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, id: event.target.value })} placeholder="例如 openai:qwen3" /></label>
                        <label><span>API 协议</span><select value={draft.api} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, api: event.target.value as ModelDraft['api'] })}><option value="">自动选择</option><option value="responses">Responses API</option><option value="chat">Chat Completions</option><option value="openai-responses">OpenAI Responses 兼容</option><option value="openai-completions">OpenAI Completions 兼容</option><option value="chat-completions">Chat Completions 兼容别名</option></select></label>
                        <label><span>API 地址</span><input value={draft.baseUrl} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, baseUrl: event.target.value })} placeholder="提供方默认或 http://127.0.0.1:11434/v1" /></label>
                        <label className="model-form-wide"><span>API 密钥环境变量</span><input value={draft.apiKeyEnv} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, apiKeyEnv: event.target.value })} placeholder="例如 OPENAI_API_KEY；本地模型可留空" /><small>Web 不读取、显示或保存密钥明文。</small></label>
                        <label><span>最大输出 Token</span><input type="number" min="1" value={String(draft.settings.max_tokens ?? '')} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, settings: { ...draft.settings, max_tokens: event.target.value ? Number(event.target.value) : undefined } })} placeholder="提供方默认" /></label>
                        <label><span>上下文窗口</span><input type="number" min="1" value={String(draft.context.window_tokens ?? '')} disabled={busy || !editable} onChange={(event) => setDraft({ ...draft, context: { ...draft.context, window_tokens: event.target.value ? Number(event.target.value) : undefined } })} placeholder="自动识别" /></label>
                      </div>
                      {draft.authKind === 'inline' && !draft.apiKeyEnv.trim() && (
                        <div className="settings-notice"><WarningCircle size={17} /><span>此模型来自含内联密钥的配置。先改为环境变量名，Web 才会创建安全覆盖。</span></div>
                      )}
                      <label className="settings-checkbox"><input type="checkbox" checked={draft.setDefault} disabled={busy || !editable || Boolean(draft.originalName && draft.setDefault)} onChange={(event) => setDraft({ ...draft, setDefault: event.target.checked })} /><span>{draft.originalName && draft.setDefault ? '当前默认模型' : '设为重启后的默认模型'}</span></label>
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
                          disabled={busy}
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
                loading={capabilityLoading}
                error={capabilityError}
                onRetry={() => void loadCapabilities()}
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
  loading,
  error,
  onRetry,
}: {
  capabilities: CapabilityInventory | null
  loading: boolean
  error: string
  onRetry: () => void
}) {
  const groups = [
    { title: 'Skills', items: capabilities?.skills ?? [], key: 'name' },
    { title: 'MCP Servers', items: capabilities?.mcp_servers ?? [], key: 'name' },
    { title: '工具', items: capabilities?.tools ?? [], key: 'name' },
  ]
  return <div className="capability-settings"><div className="settings-section-heading"><div><h2>扩展能力</h2><p>来自当前运行时的只读清单；配置编辑将在后续版本开放。</p></div></div>{loading ? <div className="settings-loading"><CircleNotch size={18} className="spin" /> 正在读取运行能力</div> : error ? <div className="settings-error" role="alert"><WarningCircle size={18} /><span>{error}</span><button type="button" onClick={onRetry}>重试</button></div> : groups.map((group) => <section key={group.title}><h3>{group.title} <small>{group.items.length}</small></h3>{group.items.length ? group.items.map((item, index) => <div className="capability-row" key={`${String(item[group.key] ?? index)}`}><span><strong>{String(item.name ?? item.reference ?? `item-${index + 1}`)}</strong><small>{String(item.description ?? item.origin ?? '')}</small></span><em>{String(item.status ?? '已载入')}</em></div>) : <p>当前没有 {group.title}。</p>}</section>)}</div>
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
  onWaive: (effect: Record<string, unknown>) => void
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
            <article className="work-record" key={String(effect.id ?? index)}>
              <strong>{String(effect.operation ?? effect.id ?? `effect-${index + 1}`)}</strong>
              <span>{String(effect.status ?? 'pending')}</span>
              <button type="button" disabled={busy} onClick={() => onWaive(effect)}>记录 waiver</button>
            </article>
          ))}
          {recoverableEffects.length > 0 && <p className="runtime-meta">可恢复 effects：{recoverableEffects.length}</p>}
        </div>
      </section>
    </div>
  )
}

function CheckpointInspector({
  checkpoints,
  busy,
  onClose,
  onFork,
}: {
  checkpoints: CheckpointRecord[]
  busy: boolean
  onClose: () => void
  onFork: (checkpoint: CheckpointRecord) => void
}) {
  const dialogRef = useModalFocus(onClose)
  return (
    <div ref={dialogRef} className="runtime-overlay" role="dialog" aria-modal="true" aria-label="Session 检查点" tabIndex={-1}>
      <section className="runtime-inspector compact-inspector">
        <header><div><strong>检查点</strong><span>Rewind 会创建新 Session，不覆盖当前工作区。</span></div><button type="button" aria-label="关闭" onClick={onClose}><X size={16} aria-hidden="true" /></button></header>
        <div className="runtime-section">
          {checkpoints.length === 0 ? <p className="runtime-empty">还没有已完成 turn。</p> : checkpoints.map((checkpoint) => (
            <article className="checkpoint-record" key={checkpoint.index}>
              <div><strong>Turn {checkpoint.index + 1}</strong><span>{checkpoint.status} · {checkpoint.receipt_count} receipts · {checkpoint.mutation_count} mutations</span><p>{checkpoint.prompt}</p></div>
              <button type="button" disabled={busy} onClick={() => onFork(checkpoint)}>从这里分支</button>
            </article>
          ))}
        </div>
      </section>
    </div>
  )
}

function TranscriptInspector({
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
  const visible = needle
    ? timeline.filter((item) => JSON.stringify(item).toLowerCase().includes(needle))
    : timeline
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

function ConversationTurn({
  turn,
  active,
  onApproval,
}: {
  turn: ConversationTurnState
  active: boolean
  onApproval: (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => void
}) {
  const presentation = turnPresentation(turn.response, turn.user?.text)
  return (
    <section className="conversation-turn">
      {turn.user && <TimelineRow item={turn.user} onApproval={onApproval} />}
      {turn.response.length > 0 && (
        <div className="assistant-turn" aria-label="Lumen 回答">
          <header className="assistant-identity">
            <span className="assistant-mark">
              <img src="/lumen-avatar.webp" alt="" width="26" height="26" />
            </span>
            <strong>Lumen</strong>
          </header>
          <div className="assistant-turn-body">
            {presentation.activity.length > 0 && (
              <TurnActivity
                active={active}
                presentation={presentation}
                onApproval={onApproval}
              />
            )}
            {presentation.foreground.map((item) => (
              <TimelineRow key={item.id} item={item} onApproval={onApproval} />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

function TurnActivity({
  active,
  presentation,
  onApproval,
}: {
  active: boolean
  presentation: TurnPresentation
  onApproval: (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => void
}) {
  const [expanded, setExpanded] = useState(active || presentation.requiresAttention)
  const current = presentation.activity.at(-1)

  useEffect(() => {
    if (active || presentation.requiresAttention) setExpanded(true)
    else setExpanded(false)
  }, [active, presentation.requiresAttention])

  return (
    <section
      className={`turn-activity ${active ? 'is-active' : ''} ${presentation.requiresAttention ? 'needs-attention' : ''}`}
      aria-label="处理过程"
    >
      <button
        className="turn-activity-summary"
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="turn-activity-heading">
          <strong>{activityTitle(presentation, active)}</strong>
          <small>{active && current ? activityItemLabel(current) : activityMeta(presentation)}</small>
        </span>
        <CaretDown size={15} className={expanded ? 'is-expanded' : ''} aria-hidden="true" />
      </button>
      {expanded && (
        <div className="turn-activity-list">
          {presentation.activity.map((item) => (
            <TimelineRow key={item.id} item={item} onApproval={onApproval} />
          ))}
        </div>
      )}
    </section>
  )
}

function PlanPanel({ plan }: { plan: Array<{ id: string; title: string; status: string; note?: string | null }> }) {
  const [expanded, setExpanded] = useState(false)
  const completed = plan.filter((step) => step.status === 'completed').length
  const current = plan.find((step) => step.status === 'in_progress')
    ?? plan.find((step) => step.status === 'blocked')
    ?? plan.find((step) => step.status === 'pending')
  return (
    <section className="web-plan" aria-label="执行计划">
      <button
        className="plan-summary"
        type="button"
        aria-expanded={expanded}
        onClick={() => setExpanded((value) => !value)}
      >
        <span className="plan-heading"><strong>计划</strong><small>{current?.title ?? '全部完成'}</small></span>
        <span className="plan-count">{completed}/{plan.length}</span>
        <CaretDown size={15} className={expanded ? 'is-expanded' : ''} />
      </button>
      <progress max={plan.length} value={completed} aria-label={`计划进度 ${completed}/${plan.length}`} />
      {expanded && (
        <ol>
          {plan.map((step) => (
            <li key={step.id} className={`is-${step.status}`}>
              <PlanStepIcon status={step.status} />
              <span>{step.title}{step.note ? <small>{step.note}</small> : null}</span>
            </li>
          ))}
        </ol>
      )}
    </section>
  )
}

function PlanStepIcon({ status }: { status: string }) {
  if (status === 'completed') return <CheckCircle size={15} weight="fill" aria-hidden="true" />
  if (status === 'in_progress') return <CircleNotch size={15} className="spin" aria-hidden="true" />
  if (status === 'blocked') return <WarningCircle size={15} weight="fill" aria-hidden="true" />
  return <Circle size={15} aria-hidden="true" />
}

function TimelineRow({
  item,
  onApproval,
}: {
  item: TimelineEntry
  onApproval: (callId: string, approved: boolean, scope?: 'once' | 'session' | 'always') => void
}) {
  const [expanded, setExpanded] = useState(false)
  const [copied, setCopied] = useState(false)

  if (item.kind === 'user') return <article className="timeline-user">{item.text}</article>
  if (item.kind === 'plan' && item.plan?.steps.length) {
    return <PlanPanel plan={item.plan.steps} />
  }
  if (item.kind === 'assistant') {
    const copy = async () => {
      await navigator.clipboard.writeText(item.text)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1000)
    }
    return (
      <article className="timeline-assistant">
        <div className="assistant-content">
          <MarkdownMessage content={item.text} />
          <button className="copy-button" type="button" onClick={copy} aria-label="复制回复">
            {copied ? <Check size={14} /> : <Copy size={14} />}
          </button>
        </div>
      </article>
    )
  }
  if (item.kind === 'tool') {
    const sourceLabel = toolSourceLabel(item)
    const compact = ['completed', 'ok', 'success'].includes(item.status ?? '')
      && !item.isError
      && !item.pendingApproval
      && !item.presentation
    return (
      <article className={`web-tool-card ${compact ? 'is-compact' : ''} ${item.isError ? 'is-error' : ''} ${item.pendingApproval ? 'is-pending' : ''}`}>
        <button
          className="tool-summary"
          type="button"
          aria-expanded={expanded}
          onClick={() => setExpanded((value) => !value)}
        >
          <ToolGlyph item={item} />
          <span>
            <span className="tool-title-line">
              <strong>{String(item.callView?.title ?? item.toolName ?? '')}</strong>
              {sourceLabel && <b>{sourceLabel}</b>}
            </span>
            <small>{String(item.callView?.detail ?? toolTarget(item))}</small>
          </span>
          <em className={`is-${item.status ?? 'idle'}`}>{toolStatus(item)}</em>
          <CaretDown size={14} className={expanded ? 'is-expanded' : ''} />
        </button>
        {item.presentation && (
          <div className="approval-request">
            <strong>{item.presentation.title}</strong>
            <pre>{expanded ? item.presentation.full_text : item.presentation.preview}</pre>
            {item.pendingApproval && item.callId && (
              <div>
                <button type="button" onClick={() => onApproval(item.callId!, true, 'once')}>允许一次</button>
                <button type="button" onClick={() => onApproval(item.callId!, true, 'session')}>本会话始终允许</button>
                <button type="button" onClick={() => onApproval(item.callId!, true, 'always')}>本项目始终允许</button>
                <button type="button" className="deny" onClick={() => onApproval(item.callId!, false)}>拒绝</button>
              </div>
            )}
          </div>
        )}
        {expanded && !item.presentation && (
          <div className="tool-detail">
            {Object.keys(item.args ?? {}).length > 0 && (
              <section><span>输入</span><pre>{JSON.stringify(item.args ?? {}, null, 2)}</pre></section>
            )}
            {(item.resultView?.full_text || item.result) && (
              <section>
                <span>输出</span>
                <pre>{String(item.resultView?.full_text ?? item.result ?? '')}</pre>
              </section>
            )}
          </div>
        )}
        {!expanded && (item.resultView?.preview || item.preview) && (
          <p>{String(item.resultView?.preview ?? item.preview ?? '')}</p>
        )}
      </article>
    )
  }
  const label = timelineNoteLabel(item.kind)
  return (
    <article className={`timeline-note is-${item.kind}`}>
      <div>
        {label && <strong>{label}</strong>}
        {item.kind === 'progress' || item.kind === 'thinking' || item.kind === 'commentary'
          ? <MarkdownMessage content={item.text} />
          : <p>{item.text}</p>}
      </div>
    </article>
  )
}

function ToolGlyph({ item }: { item: TimelineEntry }) {
  const family = String(item.callView?.family ?? '').toLowerCase()
  if (family === 'mcp') return <ArrowsClockwise size={17} />
  if (family === 'skill') return <Sparkle size={17} />
  if (family === 'web') return <MagnifyingGlass size={17} />
  const name = item.toolName ?? ''
  const normalized = name.toLowerCase()
  if (normalized.includes('search') || normalized.includes('find')) return <MagnifyingGlass size={17} />
  if (normalized.includes('edit') || normalized.includes('write') || normalized.includes('patch')) return <PencilSimpleLine size={17} />
  if (normalized.includes('list') || normalized.includes('directory')) return <FolderOpen size={17} />
  if (normalized.includes('read') || normalized.includes('file')) return <FileCode size={17} />
  return <TerminalWindow size={17} />
}

function activityItemLabel(item: TimelineEntry) {
  if (item.kind === 'tool') {
    const activeVerb = item.callView?.active_verb
    const title = item.callView?.title
    const detail = item.callView?.detail
    const label = typeof activeVerb === 'string'
      ? activeVerb
      : typeof title === 'string'
        ? title
        : item.toolName ?? '工具'
    return `${label}${typeof detail === 'string' && detail ? ` · ${detail}` : ''}`
  }
  return timelineNoteLabel(item.kind)
}

function toolStatus(item: TimelineEntry) {
  if (item.pendingApproval) return '等待审批'
  if (item.status === 'running') return '运行中'
  if (item.status === 'denied') return '已拒绝'
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
  /model [name]                     — 查看或切换模型
  /mode [manual|accept_edits|auto]  — 查看或切换审批模式
  /plan <task>                      — 先生成计划，确认后再执行
Context:
  /context                          — 查看上下文预算
  /context sources                  — 查看当前会话上下文来源
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
