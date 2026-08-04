'use client'

import {
  ArrowDown,
  ArrowsClockwise,
  CaretDown,
  Check,
  CheckCircle,
  Circle,
  CircleNotch,
  Copy,
  FileCode,
  FolderSimple,
  FolderOpen,
  Info,
  List,
  MagnifyingGlass,
  NotePencil,
  PencilSimpleLine,
  ShieldCheck,
  Sparkle,
  TerminalWindow,
  WarningCircle,
  X,
} from '@phosphor-icons/react'
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react'
import { exchangeLaunchToken, lumenApi, subscribeRun } from '@/lib/api/client'
import type {
  ApprovalMode,
  Bootstrap,
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
import { LandingEntry } from './landing-entry'
import { MarkdownMessage } from './markdown-message'

function requestId() {
  return crypto.randomUUID()
}

export function LumenApp() {
  const [bootstrap, setBootstrap] = useState<Bootstrap | null>(null)
  const [sessions, setSessions] = useState<SessionSummary[]>([])
  const [sessionId, setSessionId] = useState<string | null>(null)
  const [run, dispatch] = useReducer(runReducer, initialRunState)
  const [input, setInput] = useState('')
  const [queueMode, setQueueMode] = useState<QueueMode>('steer')
  const [loading, setLoading] = useState(true)
  const [sidebarOpen, setSidebarOpen] = useState(false)
  const [confirmAuto, setConfirmAuto] = useState(false)
  const [showJumpToLatest, setShowJumpToLatest] = useState(false)
  const closeStreamRef = useRef<(() => void) | null>(null)
  const timelineRef = useRef<HTMLElement>(null)
  const stickToLatestRef = useRef(true)

  const refreshChrome = useCallback(async () => {
    const [nextBootstrap, nextSessions] = await Promise.all([
      lumenApi.bootstrap(),
      lumenApi.listSessions(),
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
      closeStreamRef.current?.()
      closeStreamRef.current = null
      stickToLatestRef.current = true
      setShowJumpToLatest(false)
      setLoading(true)
      try {
        const snapshot = await lumenApi.session(id)
        setSessionId(id)
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
          lumenApi.listSessions(),
        ])
        if (disposed) return
        setBootstrap(nextBootstrap)
        setSessions(nextSessions)
        const requested = new URL(window.location.href).searchParams.get('session')
        const target = requested && nextSessions.some((item) => item.sessionId === requested)
          ? requested
          : null
        if (target) await openSession(target)
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
      if (name === '/mode') {
        if (!argument) {
          dispatch({
            type: 'local-message',
            message: `当前模式：${bootstrap?.approvalMode ?? 'manual'}\n\n可用：\`manual\` · \`accept_edits\` · \`plan\` · \`auto\``,
          })
          setInput('')
          return true
        }
        if (argument === 'auto') {
          setConfirmAuto(true)
        } else if (argument === 'manual' || argument === 'accept_edits' || argument === 'plan') {
          await lumenApi.updateSettings({ approvalMode: argument })
          await refreshChrome()
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
          await lumenApi.updateSettings({ model: argument })
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
      dispatch({ type: 'local-error', message: `未知命令：${name}。输入 / 查看可用命令。` })
      setInput('')
      return true
    },
    [attachRun, bootstrap, createSession, refreshChrome, run.runId, run.timeline, sessionId],
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
      setInput('')
    } catch (error) {
      dispatch({
        type: 'local-error',
        message: error instanceof Error ? error.message : '无法启动运行',
      })
    }
  }, [attachRun, createSession, handleSlashCommand, input, queueMode, run.runId, sessionId])

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
      if (mode === 'auto') {
        setConfirmAuto(true)
        return
      }
      await lumenApi.updateSettings({ approvalMode: mode })
      await refreshChrome()
    },
    [refreshChrome],
  )

  const approveAuto = useCallback(async () => {
    await lumenApi.updateSettings({ approvalMode: 'auto', confirmed: true })
    setConfirmAuto(false)
    await refreshChrome()
  }, [refreshChrome])

  const leaveSession = useCallback(() => {
    closeStreamRef.current?.()
    closeStreamRef.current = null
    setSessionId(null)
    dispatch({ type: 'reset' })
    const url = new URL(window.location.href)
    url.searchParams.delete('session')
    window.history.replaceState({}, '', `${url.pathname}${url.search}`)
  }, [])

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
  const workspaceBusy = Boolean(bootstrap?.activeRunId || run.runId)
  const isLanding = !loading && run.timeline.length === 0 && !run.runId && run.queuedInputs.length === 0
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
    }))
    const modeCommands: SlashCommand[] = [
      { value: '/mode manual', description: '每次确认', keywords: 'approval' },
      { value: '/mode accept_edits', description: '自动接受文件修改', keywords: 'approval' },
      { value: '/mode plan', description: '先出计划再执行', keywords: 'approval' },
      { value: '/mode auto', description: '自动执行', keywords: 'approval' },
    ]
    const skillCommands = (bootstrap?.skills ?? []).map((skill) => ({
      value: `/skill:${skill.name} `,
      description: skill.description || '运行技能',
      keywords: 'skill',
    }))
    return [...baseSlashCommands, ...modelCommands, ...modeCommands, ...skillCommands]
  }, [bootstrap])

  return (
    <main className="workspace-shell">
      <aside className={`session-sidebar ${sidebarOpen ? 'is-open' : ''}`}>
        <div className="sidebar-brand-row">
          <button className="brand" type="button" onClick={leaveSession}>lumen</button>
          <button className="sidebar-close" type="button" onClick={() => setSidebarOpen(false)}>
            <X size={18} />
          </button>
        </div>
        <button className="new-session" type="button" onClick={() => void createSession()}>
          <NotePencil size={18} /> 新建任务
        </button>
        <nav className="session-list" aria-label="历史任务">
          <p className="sidebar-label">任务</p>
          {sessions.length === 0 ? (
            <div className="sidebar-empty">暂无任务</div>
          ) : (
            sessions.map((session) => (
              <button
                key={session.sessionId}
                type="button"
                className={session.sessionId === sessionId ? 'is-active' : ''}
                onClick={() => void openSession(session.sessionId)}
              >
                <span>{session.title}</span>
              </button>
            ))
          )}
        </nav>
        <div className="sidebar-workspace">
          <FolderSimple size={16} />
          <span title={bootstrap?.workspace}>{bootstrap?.workspace.split('/').at(-1) ?? 'workspace'}</span>
        </div>
      </aside>

      {sidebarOpen && <button className="sidebar-scrim" type="button" onClick={() => setSidebarOpen(false)} />}

      <section className="agent-workspace">
        <header className="workspace-header">
          <div className="workspace-heading">
            <button className="sidebar-toggle" type="button" onClick={() => setSidebarOpen(true)}>
              <List size={19} />
            </button>
            <div>
              <h1>{activeSession?.title ?? '新任务'}</h1>
              {(workspaceBusy || run.status === 'failed' || run.status === 'cancelled') && <p>
                <span className={workspaceBusy ? 'status-dot is-busy' : 'status-dot'} />
                {statusLabel}{usageLabel(run.usage)}
              </p>}
            </div>
          </div>
          <div className="workspace-controls">
            <label>
              <select
                value={bootstrap?.activeModel ?? ''}
                disabled={!bootstrap || workspaceBusy}
                onChange={(event) => void lumenApi.updateSettings({ model: event.target.value }).then(refreshChrome)}
                aria-label="模型"
              >
                {bootstrap?.availableModels.map((model) => <option key={model}>{model}</option>)}
              </select>
            </label>
            <label>
              <ShieldCheck size={15} />
              <select
                value={bootstrap?.approvalMode ?? 'manual'}
                onChange={(event) => void setApprovalMode(event.target.value as ApprovalMode)}
                aria-label="审批模式"
              >
                <option value="manual">每次确认</option>
                <option value="accept_edits">自动改文件</option>
                <option value="plan">计划模式</option>
                <option value="auto">自动</option>
              </select>
            </label>
          </div>
        </header>

        {confirmAuto && (
          <div className="auto-confirm" role="alert">
            <ShieldCheck size={19} />
            <div><strong>开启 auto 模式？</strong><span>已分类的写入、执行和外部工具将不再逐次确认。</span></div>
            <button type="button" onClick={() => void approveAuto()}>确认开启</button>
            <button type="button" className="quiet" onClick={() => setConfirmAuto(false)}>取消</button>
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
                  groupTimelineByTurn(run.timeline).map((turn) => (
                    <ConversationTurn
                      key={turn.id}
                      turn={turn}
                      onApproval={(callId, approved) => {
                        if (run.runId) void lumenApi.decideApproval(run.runId, callId, approved)
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
                />
              </footer>
            </>
          )}
        </div>
      </section>
    </main>
  )
}

function EmptyWorkspace() {
  return (
    <div className="workspace-empty">
      <p>有什么需要处理？</p>
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
  onApproval,
}: {
  turn: ConversationTurnState
  onApproval: (callId: string, approved: boolean) => void
}) {
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
            {turn.response.map((item) => (
              <TimelineRow key={item.id} item={item} onApproval={onApproval} />
            ))}
          </div>
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
  onApproval: (callId: string, approved: boolean) => void
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
          <ToolGlyph name={item.toolName ?? ''} />
          <span><strong>{item.toolName}</strong><small>{toolTarget(item)}</small></span>
          <em className={`is-${item.status ?? 'idle'}`}>{toolStatus(item)}</em>
          <CaretDown size={14} className={expanded ? 'is-expanded' : ''} />
        </button>
        {item.presentation && (
          <div className="approval-request">
            <strong>{item.presentation.title}</strong>
            <pre>{expanded ? item.presentation.full_text : item.presentation.preview}</pre>
            {item.pendingApproval && item.callId && (
              <div><button type="button" onClick={() => onApproval(item.callId!, true)}>允许一次</button><button type="button" className="deny" onClick={() => onApproval(item.callId!, false)}>拒绝</button></div>
            )}
          </div>
        )}
        {expanded && !item.presentation && (
          <div className="tool-detail">
            {Object.keys(item.args ?? {}).length > 0 && (
              <section><span>输入</span><pre>{JSON.stringify(item.args ?? {}, null, 2)}</pre></section>
            )}
            {item.result && <section><span>输出</span><pre>{item.result}</pre></section>}
          </div>
        )}
        {!expanded && item.preview && <p>{item.preview}</p>}
      </article>
    )
  }
  return (
    <article className={`timeline-note is-${item.kind}`}>
      <NoteGlyph kind={item.kind} />
      <div>
        <strong>{noteLabel(item.kind)}</strong>
        {item.kind === 'progress'
          ? <MarkdownMessage content={item.text} />
          : <p>{item.text}</p>}
      </div>
    </article>
  )
}

function ToolGlyph({ name }: { name: string }) {
  const normalized = name.toLowerCase()
  if (normalized.includes('search') || normalized.includes('find')) return <MagnifyingGlass size={17} />
  if (normalized.includes('edit') || normalized.includes('write') || normalized.includes('patch')) return <PencilSimpleLine size={17} />
  if (normalized.includes('list') || normalized.includes('directory')) return <FolderOpen size={17} />
  if (normalized.includes('read') || normalized.includes('file')) return <FileCode size={17} />
  return <TerminalWindow size={17} />
}

function NoteGlyph({ kind }: { kind: TimelineEntry['kind'] }) {
  if (kind === 'commentary') return <Sparkle size={15} />
  if (kind === 'progress') return <CircleNotch size={15} />
  if (kind === 'compaction') return <ArrowsClockwise size={15} />
  if (kind === 'error') return <WarningCircle size={15} weight="fill" />
  return <Info size={15} />
}

function noteLabel(kind: TimelineEntry['kind']) {
  if (kind === 'commentary') return '进展'
  if (kind === 'progress') return '进度'
  if (kind === 'compaction') return '上下文'
  if (kind === 'error') return '错误'
  return '系统'
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
Model:
  /model [name]                     — 查看或切换模型
  /mode [manual|accept_edits|plan|auto] — 查看或切换审批模式
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
