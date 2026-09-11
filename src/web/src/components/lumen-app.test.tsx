// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { lumenApi, subscribeRun } from '../lib/api/client'
import type { Bootstrap, ConfigurationSnapshot, SessionSnapshot } from '../lib/api/types'
import { LumenApp } from './lumen-app'

vi.mock('../lib/api/client', () => ({
  exchangeLaunchToken: vi.fn().mockResolvedValue(undefined),
  subscribeRun: vi.fn(() => () => {}),
  lumenApi: {
    readAttachment: vi.fn(async () => new Blob()),
    bootstrap: vi.fn(), listSessions: vi.fn(), createSession: vi.fn(), session: vi.fn(),
    updateSessionSettings: vi.fn(), updateWorkspaceSettings: vi.fn(), startRun: vi.fn(),
    forkSession: vi.fn(), renameSession: vi.fn(), cancelRun: vi.fn(), invokeSkill: vi.fn(), invokePrompt: vi.fn(),
    configuration: vi.fn(), capabilities: vi.fn(), upsertModelConfiguration: vi.fn(), deleteModelConfiguration: vi.fn(),
    setMcpServerEnabled: vi.fn(),
    inspectReasoning: vi.fn(),
    listAgents: vi.fn(), waiveVerification: vi.fn(), reviewPlan: vi.fn(),
  },
}))
vi.mock('./mascot/mascot-scene', () => ({ MascotScene: () => null }))
vi.mock('./live-voice-controls', () => ({ LiveVoiceControls: () => null }))

let root: Root
let container: HTMLDivElement
let workspace: Bootstrap
let snapshots: Map<string, SessionSnapshot>

function snapshot(sessionId: string): SessionSnapshot {
  return {
    sessionId, modelId: 'model-a', createdAt: '2026-09-03T00:00:00Z',
    approvalMode: 'manual', collaborationMode: 'default', activeRunId: null,
    plan: { goal: '', revision: 0, state_version: 0, lifecycle: 'draft', approved_revision: null, steps: [] },
    timeline: [], lastUserInput: null, planReviewStatus: 'none', transcriptDensity: 'normal',
    pendingClarification: null, workProducts: [], pendingEffects: [], recoverableEffects: [],
    agents: [], agentUsage: {}, liveSessions: [],
  }
}

function button(label: string): HTMLButtonElement {
  const result = Array.from(document.querySelectorAll('button')).find((item) =>
    item.getAttribute('aria-label') === label || item.textContent?.trim() === label)
  if (!result) throw new Error(`Missing button: ${label}`)
  return result
}

async function click(target: HTMLElement) {
  await act(async () => { target.click() })
}

async function choose(label: string, option: string) {
  await click(button(label))
  const item = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
    .find((node) => node.querySelector('strong')?.textContent === option)
  if (!item) throw new Error(`Missing option: ${option}`)
  await click(item)
}

async function type(value: string) {
  const input = document.querySelector('textarea')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, value)
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

async function mount() {
  await act(async () => { root.render(<LumenApp />) })
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.stubGlobal('IS_REACT_ACT_ENVIRONMENT', true)
  vi.stubGlobal('ResizeObserver', class { observe() {} disconnect() {} })
  HTMLElement.prototype.scrollIntoView = vi.fn()
  HTMLElement.prototype.scrollTo = vi.fn()
  window.history.replaceState({}, '', '/')
  localStorage.clear()
  snapshots = new Map()
  workspace = {
    agent: 'test', workspace: '/test', activeModel: 'model-a', modelId: 'model-a',
    inputModalities: ['text'], availableModels: ['model-a', 'model-b'],
    approvalMode: 'manual', collaborationMode: 'default', activeRunId: null,
    tools: [], skills: [], mcp: [], warnings: [], liveEnabled: false,
  }
  vi.mocked(lumenApi.bootstrap).mockImplementation(async () => ({ ...workspace }))
  vi.mocked(lumenApi.listSessions).mockImplementation(async () => Array.from(snapshots.keys()).map((id) => ({
    sessionId: id, title: id, modelId: 'model-a', createdAt: '2026-09-03T00:00:00Z', archived: false,
  })))
  vi.mocked(lumenApi.createSession).mockImplementation(async () => {
    const sessionId = `session-${snapshots.size + 1}`
    snapshots.set(sessionId, snapshot(sessionId))
    return { sessionId }
  })
  vi.mocked(lumenApi.session).mockImplementation(async (id) => ({ ...snapshots.get(id)! }))
  vi.mocked(lumenApi.listAgents).mockResolvedValue([])
  vi.mocked(lumenApi.inspectReasoning).mockResolvedValue({ reasoning: {
    requested: null, effective: null, source: 'provider_default', mapping: 'provider_default',
    parameters: {}, supported_levels: ['provider_default'], capability_status: 'unknown', level_map: {},
  } })
  vi.mocked(lumenApi.updateSessionSettings).mockImplementation(async (id, settings) => {
    Object.assign(snapshots.get(id)!, settings)
    return { status: 'updated' }
  })
  vi.mocked(lumenApi.updateWorkspaceSettings).mockImplementation(async ({ model }) => {
    workspace.activeModel = model!
    return { status: 'updated' }
  })
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  vi.useRealTimers()
})

describe('plan mode review and ordinary progress', () => {
  function plannedSession(mode: 'default' | 'plan' = 'plan') {
    const source = snapshot('source')
    source.collaborationMode = mode
    source.plan = { goal: '改进交互', revision: 3, state_version: 4, lifecycle: mode === 'plan' ? 'review_pending' : 'executing', approved_revision: null,
      steps: [{ id: 'inspect', title: '检查现状', status: 'in_progress', depends_on: [], acceptance_criteria: [] }] }
    source.planReviewStatus = mode === 'plan' ? 'review_pending' : 'none'
    source.timeline = [{ id: 'user', kind: 'user', text: '改进交互' }, { id: 'plan', kind: 'plan', plan: source.plan }]
    snapshots.set('source', source)
    window.history.replaceState({}, '', '?session=source')
    return source
  }

  it('shows a read-only progress popover without approval in ordinary mode', async () => {
    plannedSession('default').activeRunId = 'running'
    await mount()
    expect(document.querySelector('[aria-label="确认方案"]')).toBeNull()
    await click(button('查看任务进度'))
    expect(document.querySelector('[role="dialog"][aria-label="任务进度"]')).not.toBeNull()
    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' })) })
    expect(document.querySelector('[role="dialog"][aria-label="任务进度"]')).toBeNull()
    expect(document.activeElement).toBe(button('查看任务进度'))
    await click(button('查看任务进度'))
    await act(async () => { window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', isComposing: true })) })
    expect(document.querySelector('[role="dialog"][aria-label="任务进度"]')).not.toBeNull()
    await act(async () => { container.querySelector('textarea')!.focus() })
    expect(document.querySelector('[role="dialog"][aria-label="任务进度"]')).toBeNull()
    expect(lumenApi.reviewPlan).not.toHaveBeenCalled()
    const receive = vi.mocked(subscribeRun).mock.calls[0][1]
    await act(async () => { receive({ version: 1, sequence: 1, sessionId: 'source', runId: 'running',
      type: 'run.completed', createdAt: '2026-09-04T00:00:00Z', data: {} }) })
    expect(document.querySelector('[aria-label="查看任务进度"]')).toBeNull()
  })

  it('does not retain a capsule or a plan sidebar on a completed task', async () => {
    plannedSession('default')
    await mount()
    expect(document.querySelector('[aria-label="查看任务进度"]')).toBeNull()
    expect(document.querySelector('[aria-label="打开任务计划"]')).toBeNull()
    expect(document.querySelector('.plan-drawer')).toBeNull()
  })

  it('does not present a previous turn plan as progress of a new question', async () => {
    const source = plannedSession('default')
    source.timeline.push({ id: 'next', kind: 'user', text: '你好' })
    source.activeRunId = 'next-run'
    await mount()
    expect(document.querySelector('[aria-label="查看任务进度"]')).toBeNull()
    expect(container.textContent).not.toContain('正在处理')
    expect(container.querySelector('.turn-live-placeholder .thinking-orb')).not.toBeNull()
  })

  it('approves the exact revision once and attaches the accepted run', async () => {
    plannedSession()
    let resolve!: (result: Awaited<ReturnType<typeof lumenApi.reviewPlan>>) => void
    vi.mocked(lumenApi.reviewPlan).mockReturnValue(new Promise((done) => { resolve = done }))
    await mount()
    expect(container.querySelector('[aria-label="待确认方案"]')?.textContent).toContain('检查现状')
    const approve = button('确认并开始执行')
    await act(async () => { approve.click(); approve.click() })
    expect(lumenApi.reviewPlan).toHaveBeenCalledExactlyOnceWith('source', 'approve', 3, expect.any(String), '')
    await act(async () => { resolve({ runId: 'approved-run', sessionId: 'source', status: 'started' }) })
    expect(subscribeRun).toHaveBeenCalledWith('approved-run', expect.any(Function), expect.any(Function))
    expect(container.querySelector('.composer-plan-indicator')).toBeNull()
  })

  it('keeps feedback on rejection failure and sends it with the reviewed revision', async () => {
    plannedSession()
    vi.mocked(lumenApi.reviewPlan).mockRejectedValueOnce(new Error('版本已改变，请刷新'))
    await mount()
    await click(button('先调整方案'))
    const input = container.querySelector<HTMLTextAreaElement>('[aria-label="计划修改意见"]')!
    expect(document.activeElement).toBe(input)
    expect(button('发送意见并重新规划').disabled).toBe(true)
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, '先覆盖键盘操作')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(button('发送意见并重新规划'))
    expect(lumenApi.reviewPlan).toHaveBeenCalledWith('source', 'reject', 3, expect.any(String), '先覆盖键盘操作')
    expect(input.value).toBe('先覆盖键盘操作')
    expect(container.textContent).toContain('版本已改变，请刷新')
    expect(container.querySelector('.composer-plan-indicator')?.textContent).toContain('Plan')
    vi.mocked(lumenApi.reviewPlan).mockResolvedValueOnce({ runId: 'replanning-run', sessionId: 'source', status: 'started' })
    await click(button('发送意见并重新规划'))
    expect(subscribeRun).toHaveBeenCalledWith('replanning-run', expect.any(Function), expect.any(Function))
    expect(container.querySelector('.composer-plan-indicator')?.textContent).toContain('Plan')
  })

  it('does not attach an old review response after navigating to another task', async () => {
    plannedSession()
    snapshots.set('other', snapshot('other'))
    let resolve!: (result: Awaited<ReturnType<typeof lumenApi.reviewPlan>>) => void
    vi.mocked(lumenApi.reviewPlan).mockReturnValue(new Promise((done) => { resolve = done }))
    await mount()
    await click(button('确认并开始执行'))
    await click(button('other'))
    await act(async () => { resolve({ runId: 'old-run', sessionId: 'source', status: 'started' }) })
    expect(subscribeRun).not.toHaveBeenCalled()
    expect(window.location.search).toBe('?session=other')
    expect(container.querySelector('[aria-label="确认方案"]')).toBeNull()
  })
})

describe('external result recovery', () => {
  it('requires an explicit scoped reason, preserves it on failure, and refreshes after confirmation', async () => {
    const source = snapshot('source')
    source.pendingEffects = [{ id: 'effect:search', operation: 'exa_search', status: 'reconciliation_required' }]
    snapshots.set('source', source)
    window.history.replaceState({}, '', '?session=source')
    vi.mocked(lumenApi.waiveVerification).mockRejectedValueOnce(new Error('保存失败'))
      .mockImplementationOnce(async () => { source.pendingEffects = []; return { status: 'waived' } })
    await mount()
    await click(button('查看并处理'))
    expect(button('记录人工确认').disabled).toBe(true)
    const input = document.querySelector<HTMLInputElement>('input[placeholder="说明已检查的结果及可接受的依据"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, '已检查此次检索结果')
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
    await click(button('记录人工确认'))
    expect(document.body.textContent).toContain('保存失败')
    expect(input.value).toBe('已检查此次检索结果')
    await click(button('记录人工确认'))
    expect(lumenApi.waiveVerification).toHaveBeenLastCalledWith('source', ['effect:search'], '已检查此次检索结果')
    expect(document.body.textContent).toContain('没有待验证副作用')
    expect(lumenApi.startRun).not.toHaveBeenCalled()
    expect(lumenApi.forkSession).not.toHaveBeenCalled()
  })
})

describe('settings editing', () => {
  let configuration: ConfigurationSnapshot
  beforeEach(() => {
    configuration = {
      revision: 'r1', targetPath: '/test/.lumen/agent.web.yaml', editable: true, editReason: null,
      exclusive: false, sources: [], warnings: [], defaultModel: 'model-a', activeModel: 'model-a',
      mcpServers: [{ name: 'exa', enabled: true, source: null }],
      models: ['model-a', 'model-b'].map((name) => ({
        name, id: `openai:${name}`, api: 'responses', baseUrl: null, apiKeyEnv: null,
        settings: {}, context: {}, inputModalities: ['text'], isDefault: name === 'model-a',
        source: null, authKind: 'none', authAvailable: false,
      })),
    }
    vi.mocked(lumenApi.configuration).mockImplementation(async () => configuration)
    vi.mocked(lumenApi.capabilities).mockResolvedValue({ tools: [], skills: [], mcp_servers: [], agent_profiles: [] })
  })

  async function editModelId(value: string) {
    const input = document.querySelector<HTMLInputElement>('input[placeholder="例如 openai:qwen3"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(input, value)
      input.dispatchEvent(new Event('input', { bubbles: true }))
    })
  }

  it('preserves model edits across categories and requires explicit discard even after repeated Escape', async () => {
    await mount()
    await click(button('打开系统设置'))
    await editModelId('openai:updated')
    expect(button('添加模型').disabled).toBe(true)
    expect(document.querySelector<HTMLSelectElement>('select[aria-label="已配置模型"]')?.disabled).toBe(true)
    await click(button('通用设置'))
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(document.querySelector('[role="alert"]')?.textContent).toContain('有尚未保存的更改')
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(document.querySelector('.settings-dialog')).not.toBeNull()
    await click(button('继续编辑'))
    expect(document.querySelector<HTMLInputElement>('input[placeholder="例如 openai:qwen3"]')?.value).toBe('openai:updated')
    await click(button('关闭设置'))
    await click(button('放弃更改'))
    expect(document.querySelector('.settings-dialog')).toBeNull()
    expect(lumenApi.upsertModelConfiguration).not.toHaveBeenCalled()
  })

  it('saves through the existing revision contract and preserves a failed draft for retry', async () => {
    await mount()
    await click(button('打开系统设置'))
    await editModelId('openai:updated')
    vi.mocked(lumenApi.upsertModelConfiguration).mockRejectedValueOnce(new Error('配置已经更新，请重新读取'))
    await act(async () => { document.querySelector('.model-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
    expect(document.querySelector('.settings-inline-error')?.textContent).toContain('配置已经更新')
    expect(document.querySelector<HTMLInputElement>('input[placeholder="例如 openai:qwen3"]')?.value).toBe('openai:updated')
    vi.mocked(lumenApi.upsertModelConfiguration).mockResolvedValue({
      ...configuration, status: 'saved', revision: 'r2', restartRequired: false,
      models: configuration.models.map((model) => model.name === 'model-a' ? { ...model, id: 'openai:updated' } : model),
    })
    await act(async () => { document.querySelector('.model-form')!.dispatchEvent(new Event('submit', { bubbles: true, cancelable: true })) })
    expect(lumenApi.upsertModelConfiguration).toHaveBeenLastCalledWith('model-a', expect.objectContaining({expectedRevision: 'r1', id: 'openai:updated', setDefault: true}))
    expect(button('已保存').disabled).toBe(true)
    expect(document.querySelector('.settings-notice.is-success')?.textContent).toContain('配置已生效')
  })

  it('keeps configuration read-only while a task is running', async () => {
    workspace.activeRunId = 'busy'
    await mount()
    await click(button('打开系统设置'))
    expect(button('添加模型').disabled).toBe(true)
    expect(document.querySelector<HTMLInputElement>('input[placeholder="例如 openai:qwen3"]')?.disabled).toBe(true)
    expect(document.querySelector('[role="switch"]')?.hasAttribute('disabled')).toBe(true)
    expect(lumenApi.upsertModelConfiguration).not.toHaveBeenCalled()
  })

  it('preserves a proxy capability profile through preview and save', async () => {
    configuration.models[0].reasoningProfile = 'openai-gpt56-sol'
    await mount()
    await click(button('打开系统设置'))
    expect(document.querySelector<HTMLInputElement>('input[aria-label="代理能力 Profile"]')?.value)
      .toBe('openai-gpt56-sol')
    expect(lumenApi.inspectReasoning).toHaveBeenLastCalledWith(expect.objectContaining({
      reasoningProfile: 'openai-gpt56-sol',
    }))
    await editModelId('openai:gpt-5.6')
    vi.mocked(lumenApi.upsertModelConfiguration).mockResolvedValue({
      ...configuration, status: 'saved', revision: 'r2', restartRequired: false,
    })
    await act(async () => { document.querySelector('.model-form')!.dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    ) })
    expect(lumenApi.upsertModelConfiguration).toHaveBeenLastCalledWith('model-a', expect.objectContaining({
      reasoningProfile: 'openai-gpt56-sol', id: 'openai:gpt-5.6',
    }))
  })

  it('saves provider-native search independently from external MCP activation', async () => {
    vi.mocked(lumenApi.capabilities).mockResolvedValue({
      tools: [], skills: [], agent_profiles: [],
      mcp_servers: [{ name: 'exa', status: 'ok', enabled: true }],
    })
    await mount()
    await click(button('打开系统设置'))
    const nativeSearch = document.querySelector<HTMLSelectElement>('select[aria-label="模型内建联网"]')!
    await act(async () => {
      nativeSearch.value = 'disabled'
      nativeSearch.dispatchEvent(new Event('change', { bubbles: true }))
    })
    vi.mocked(lumenApi.upsertModelConfiguration).mockResolvedValue({
      ...configuration, status: 'saved', revision: 'r2', restartRequired: false,
      models: configuration.models.map((model) => ({
        ...model,
        nativeWebSearch: { mode: 'disabled', search_context_size: 'medium' },
        nativeWebSearchEnabled: false,
      })),
    })
    await act(async () => { document.querySelector('.model-form')!.dispatchEvent(
      new Event('submit', { bubbles: true, cancelable: true }),
    ) })
    expect(lumenApi.upsertModelConfiguration).toHaveBeenLastCalledWith(
      'model-a',
      expect.objectContaining({ nativeWebSearch: { mode: 'disabled', search_context_size: 'medium' } }),
    )

    const disabledConfiguration = {
      ...configuration,
      revision: 'r2',
      mcpServers: [{ name: 'exa', enabled: false, source: null }],
      restartRequired: true,
    }
    vi.mocked(lumenApi.setMcpServerEnabled).mockResolvedValue({
      ...disabledConfiguration,
      status: 'saved',
    })
    await click(button('扩展能力'))
    const exa = document.querySelector<HTMLInputElement>('input[aria-label="exa MCP"]')!
    expect(exa.checked).toBe(true)
    await click(exa)
    expect(lumenApi.setMcpServerEnabled).toHaveBeenCalledWith('exa', 'r2', false)
    expect(document.querySelector<HTMLInputElement>('input[aria-label="exa MCP"]')?.checked).toBe(false)
  })

  it('uses backend capability choices in settings and ignores a stale preview after editing the model', async () => {
    let finishOld: ((value: Awaited<ReturnType<typeof lumenApi.inspectReasoning>>) => void) | undefined
    vi.mocked(lumenApi.inspectReasoning).mockImplementationOnce(() => new Promise((resolve) => { finishOld = resolve }))
    vi.mocked(lumenApi.inspectReasoning).mockResolvedValue({ reasoning: {
      requested: null, effective: null, source: 'provider_default', mapping: 'provider_default', parameters: {},
      supported_levels: ['provider_default', 'low', 'high', 'max'], capability_status: 'supported',
      level_map: { low: 'low', high: 'high', max: 'max' },
    } })
    await mount()
    await click(button('打开系统设置'))
    expect(document.querySelector<HTMLSelectElement>('select[aria-label="默认推理强度"]')?.disabled).toBe(true)
    await editModelId('openai:k3')
    const select = document.querySelector<HTMLSelectElement>('select[aria-label="默认推理强度"]')!
    expect(Array.from(select.options).map((option) => option.value)).toEqual(['', 'provider_default', 'low', 'high', 'max'])
    await act(async () => { finishOld?.({ reasoning: {
      requested: null, effective: null, source: 'provider_default', mapping: 'provider_default', parameters: {},
      supported_levels: ['provider_default', 'off'], capability_status: 'supported',
    } }) })
    expect(Array.from(select.options).map((option) => option.value)).not.toContain('off')
    expect(lumenApi.inspectReasoning).toHaveBeenLastCalledWith(expect.objectContaining({ id: 'openai:k3' }))
  })
})

describe('asynchronous conversation titles', () => {
  it('opens a positioned rename menu and rejects a late title poll after manual naming', async () => {
    vi.useFakeTimers()
    snapshots.set('pending', snapshot('pending'))
    window.history.replaceState({}, '', '/?session=pending')
    const summary = { sessionId: 'pending', title: '新对话', titlePending: true,
      modelId: 'model-a', createdAt: '2026-09-03T00:00:00Z', archived: false }
    vi.mocked(lumenApi.listSessions).mockResolvedValue([summary])
    await mount()
    await click(button('管理任务：新对话'))
    const menu = document.querySelector<HTMLElement>('[role="menu"]')!
    expect(menu.style.visibility).toBe('visible')
    await click(menu.querySelector<HTMLButtonElement>('[role="menuitem"]')!)
    const title = document.querySelector<HTMLInputElement>('[role="dialog"] input')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(title, '我的分享')
      title.dispatchEvent(new Event('input', { bubbles: true }))
    })
    let release!: (items: typeof summary[]) => void
    vi.mocked(lumenApi.listSessions).mockImplementationOnce(() => new Promise((resolve) => { release = resolve }))
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    vi.mocked(lumenApi.listSessions).mockResolvedValue([{ ...summary, title: '我的分享', titlePending: false }])
    await click(button('保存标题'))
    expect(lumenApi.renameSession).toHaveBeenCalledWith('pending', '我的分享')
    await act(async () => release([{ ...summary, title: '迟到的自动标题', titlePending: false }]))
    expect(container.querySelector('h1')?.textContent).toBe('我的分享')
  })

  it('updates the sidebar, header and browser title without replacing the conversation or draft', async () => {
    vi.useFakeTimers()
    snapshots.set('pending', snapshot('pending'))
    window.history.replaceState({}, '', '/?session=pending')
    const summary = { sessionId: 'pending', title: '新对话', titlePending: true,
      modelId: 'model-a', createdAt: '2026-09-03T00:00:00Z', archived: false }
    vi.mocked(lumenApi.listSessions).mockResolvedValue([summary])
    await mount()
    await type('保留草稿')
    expect(container.querySelector('h1')?.textContent).toBe('新对话')
    const snapshotCalls = vi.mocked(lumenApi.session).mock.calls.length
    vi.mocked(lumenApi.listSessions).mockResolvedValue([{ ...summary, title: '架构讲解提纲', titlePending: false }])
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(container.querySelector('h1')?.textContent).toBe('架构讲解提纲')
    expect(document.title).toBe('架构讲解提纲 · Lumen')
    expect(container.querySelector('textarea')?.value).toBe('保留草稿')
    expect(lumenApi.session).toHaveBeenCalledTimes(snapshotCalls)
    const polls = vi.mocked(lumenApi.listSessions).mock.calls.length
    await act(async () => { await vi.advanceTimersByTimeAsync(6000) })
    expect(lumenApi.listSessions).toHaveBeenCalledTimes(polls)
    expect(lumenApi.startRun).not.toHaveBeenCalled()
  })

  it('retries failed metadata requests while leaving message content usable', async () => {
    vi.useFakeTimers()
    snapshots.set('pending', snapshot('pending'))
    window.history.replaceState({}, '', '/?session=pending')
    const summary = { sessionId: 'pending', title: '新对话', titlePending: true,
      modelId: 'model-a', createdAt: '2026-09-03T00:00:00Z', archived: false }
    vi.mocked(lumenApi.listSessions).mockResolvedValueOnce([summary])
      .mockRejectedValueOnce(new Error('metadata offline'))
      .mockResolvedValue([{ ...summary, title: '已恢复标题', titlePending: false }])
    await mount()
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(container.querySelector('h1')?.textContent).toBe('新对话')
    expect(container.textContent).not.toContain('metadata offline')
    await act(async () => { await vi.advanceTimersByTimeAsync(1500) })
    expect(container.querySelector('h1')?.textContent).toBe('已恢复标题')
  })
})

describe('sidebar navigation', () => {
  it('keeps the Kimi brand after selecting an OpenAI-compatible model', async () => {
    workspace.availableModels = ['model-a', 'kimi-k3']
    workspace.modelId = 'openai:proxy-model'
    await mount()
    await click(button('模型：model-a'))
    const option = Array.from(document.querySelectorAll<HTMLElement>('[role="option"]'))
      .find((node) => node.querySelector('strong')?.textContent === 'kimi-k3')!
    const kimiIcon = option.querySelector('svg')!.innerHTML
    await click(option)
    expect(button('模型：kimi-k3').querySelector('svg')!.innerHTML).toBe(kimiIcon)
    await click(button('模型：kimi-k3'))
    const selected = document.querySelector('[role="option"][aria-selected="true"]')!
    expect(selected.querySelector('svg')!.innerHTML).toBe(kimiIcon)
  })

  function qwenReasoning(): NonNullable<Bootstrap['reasoning']> {
    return { requested: null, effective: null, source: 'provider_default', mapping: 'provider_default',
      supported_levels: ['provider_default', 'off', 'low', 'medium', 'high', 'xhigh', 'max'],
      parameters: {}, capability_status: 'supported', provider_default_level: 'xhigh',
      level_map: { off: 'off', low: 'low', medium: 'medium', high: 'xhigh', xhigh: 'xhigh', max: 'xhigh' } }
  }

  function kimiReasoning(): NonNullable<Bootstrap['reasoning']> {
    return { requested: null, effective: null, source: 'provider_default', mapping: 'provider_default',
      supported_levels: ['provider_default', 'low', 'medium', 'high', 'xhigh', 'max'],
      parameters: {}, capability_status: 'supported', provider_default_level: 'high',
      level_map: { low: 'low', medium: 'high', high: 'high', xhigh: 'max', max: 'max' } }
  }

  it('clears old Session thinking on the new-task page and switches native choices with the model', async () => {
    workspace.reasoning = qwenReasoning()
    const old = snapshot('old')
    old.reasoning = { ...qwenReasoning(), requested: 'low', effective: 'low', source: 'session' }
    snapshots.set('old', old)
    window.history.replaceState({}, '', '/?session=old')
    await mount()
    expect(button('推理强度：low')).toBeDefined()
    await click(button('新建任务'))
    await click(button('推理强度：跟随供应商默认（xhigh）'))
    expect(Array.from(document.querySelectorAll('[role="option"] strong'), (item) => item.textContent))
      .toEqual(['跟随供应商默认（xhigh）', 'off', 'low', 'medium', 'xhigh'])
    await click(button('推理强度：跟随供应商默认（xhigh）'))
    vi.mocked(lumenApi.updateWorkspaceSettings).mockImplementation(async ({ model }) => {
      workspace.activeModel = model!
      workspace.reasoning = kimiReasoning()
      return { status: 'updated' }
    })
    await choose('模型：model-a', 'model-b')
    expect(button('推理强度：跟随供应商默认（high）').textContent).toContain('默认 · high')
    await click(button('推理强度：跟随供应商默认（high）'))
    expect(Array.from(document.querySelectorAll('[role="option"] strong'), (item) => item.textContent))
      .toEqual(['跟随供应商默认（high）', 'low', 'high', 'max'])
    expect(document.body.textContent).toContain('model-b · 用于下一轮对话')
    expect(document.body.textContent).toContain('不发送推理参数')
  })

  it('refreshes the active Session choices after switching models', async () => {
    workspace.reasoning = qwenReasoning()
    const current = snapshot('current')
    current.reasoning = qwenReasoning()
    snapshots.set('current', current)
    window.history.replaceState({}, '', '/?session=current')
    await mount()
    vi.mocked(lumenApi.updateWorkspaceSettings).mockImplementation(async ({ model }) => {
      workspace.activeModel = model!
      workspace.reasoning = kimiReasoning()
      current.reasoning = { ...kimiReasoning(), requested: 'max', effective: 'max', source: 'session' }
      return { status: 'updated' }
    })
    await choose('模型：model-a', 'model-b')
    expect(button('推理强度：max')).toBeDefined()
    await click(button('推理强度：max'))
    expect(Array.from(document.querySelectorAll('[role="option"] strong'), (item) => item.textContent))
      .toEqual(['跟随供应商默认（high）', 'low', 'high', 'max'])
  })

  it('ignores a delayed model-switch Session response after returning to a new task', async () => {
    workspace.reasoning = qwenReasoning()
    const old = snapshot('old')
    old.reasoning = qwenReasoning()
    snapshots.set('old', old)
    window.history.replaceState({}, '', '/?session=old')
    await mount()
    let finish: (value: SessionSnapshot) => void = () => { throw new Error('request not started') }
    vi.mocked(lumenApi.session).mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    vi.mocked(lumenApi.updateWorkspaceSettings).mockImplementation(async ({ model }) => {
      workspace.activeModel = model!
      workspace.reasoning = kimiReasoning()
      return { status: 'updated' }
    })
    await choose('模型：model-a', 'model-b')
    await click(button('新建任务'))
    await act(async () => { finish(old) })
    expect(button('推理强度：跟随供应商默认（high）')).toBeDefined()
  })

  it.each([
    ['unknown', '推理控制未配置'], ['unsupported', '不支持调节'],
  ] as const)('disables a single default choice for %s capability', async (status, label) => {
    workspace.reasoning = { requested: null, effective: null, source: 'provider_default', mapping: 'provider_default',
      supported_levels: ['provider_default'], parameters: {}, capability_status: status }
    await mount()
    expect(button(`推理强度：${label}`).disabled).toBe(true)
  })

  it('displays the effective provider alias in the thinking selector', async () => {
    workspace.reasoning = { requested: 'medium', effective: 'high', source: 'model', mapping: 'native',
      supported_levels: ['provider_default', 'low', 'medium', 'high'], parameters: {},
      capability_status: 'supported', level_map: { low: 'low', medium: 'high', high: 'high' } }
    await mount()
    expect(button('推理强度：medium → high')).toBeDefined()
  })

  it('keeps a deployment alias selectable when its native target was excluded by configuration', async () => {
    workspace.reasoning = { ...kimiReasoning(), supported_levels: ['provider_default', 'medium'],
      level_map: { medium: 'high' } }
    await mount()
    await click(button('推理强度：跟随供应商默认（high）'))
    expect(Array.from(document.querySelectorAll('[role="option"] strong'), (item) => item.textContent))
      .toEqual(['跟随供应商默认（high）', 'medium → high'])
  })

  it('uses Session thinking choices and saves through the shared settings API without losing the draft', async () => {
    workspace.reasoning = { requested: 'medium', effective: 'medium', source: 'model', mapping: 'native',
      supported_levels: ['provider_default', 'low', 'medium', 'high'], parameters: {} }
    const session = snapshot('discussion')
    session.reasoning = { ...workspace.reasoning, requested: 'high', effective: 'high', source: 'session' }
    snapshots.set('discussion', session)
    window.history.replaceState({}, '', '/?session=discussion')
    vi.mocked(lumenApi.updateSessionSettings).mockImplementation(async (id, settings) => {
      const current = snapshots.get(id)!
      current.reasoning = { ...workspace.reasoning!, requested: settings.reasoningEffort!,
        effective: settings.reasoningEffort!, source: 'session' }
      return { status: 'updated' }
    })
    await mount()
    await type('保留草稿')
    await choose('推理强度：high', 'low')
    expect(lumenApi.updateSessionSettings).toHaveBeenCalledWith('discussion', { reasoningEffort: 'low' })
    expect(button('推理强度：low')).toBeDefined()
    expect(container.querySelector('textarea')?.value).toBe('保留草稿')
  })

  it('disables thinking changes during a run and reports unverified legacy settings honestly', async () => {
    workspace.activeRunId = 'running'
    workspace.reasoning = { requested: null, effective: null, source: 'legacy_settings', mapping: 'unverified',
      supported_levels: ['provider_default', 'low'], parameters: {} }
    await mount()
    expect(button('推理强度：原始配置（未校验）').disabled).toBe(true)
  })

  it('filters configured models, ignores IME confirmation, and switches without losing the draft', async () => {
    await mount()
    await type('保留模型切换前的草稿')
    await click(button('模型：model-a'))
    const search = document.querySelector<HTMLInputElement>('input[aria-label="搜索模型"]')!
    await act(async () => {
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')!.set!.call(search, 'model-b')
      search.dispatchEvent(new Event('input', { bubbles: true }))
    })
    expect(document.querySelectorAll('[role="option"]')).toHaveLength(1)
    expect(document.querySelector('[role="option"] strong')?.textContent).toBe('model-b')
    search.focus()
    await act(async () => { search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', isComposing: true, bubbles: true })) })
    expect(lumenApi.updateWorkspaceSettings).not.toHaveBeenCalled()
    await act(async () => { search.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })) })
    expect(lumenApi.updateWorkspaceSettings).toHaveBeenCalledWith({ model: 'model-b' })
    expect(button('模型：model-b')).toBeDefined()
    expect(container.querySelector('textarea')?.value).toBe('保留模型切换前的草稿')
    expect(document.querySelector('[role="listbox"]')).toBeNull()
  })

  it('opens search from the rail without expanding the sidebar or changing the conversation draft', async () => {
    vi.useFakeTimers()
    snapshots.set('discussion', snapshot('discussion'))
    window.history.replaceState({}, '', '/?session=discussion')
    await mount()
    await type('尚未发送的草稿')
    await click(button('收起侧边栏'))
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(container.querySelector('main')?.classList.contains('is-sidebar-collapsed')).toBe(true)
    expect(localStorage.getItem('lumen.sidebarCollapsed')).toBe('true')
    expect(container.querySelector('textarea')?.value).toBe('尚未发送的草稿')
    expect(window.location.search).toBe('?session=discussion')
    const search = container.querySelector<HTMLButtonElement>('.sidebar-rail [aria-label="搜索任务"]')!
    search.focus()
    await click(search)
    await act(async () => { await vi.advanceTimersByTimeAsync(20) })
    expect(container.querySelector('main')?.classList.contains('is-sidebar-collapsed')).toBe(true)
    expect(document.activeElement).toBe(container.querySelector('.session-search-header input'))
    expect(container.querySelector('.agent-workspace')?.hasAttribute('inert')).toBe(true)
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(container.querySelector('[aria-label="搜索对话"]')).toBeNull()
    expect(document.activeElement).toBe(search)
    expect(container.querySelector('.agent-workspace')?.hasAttribute('inert')).toBe(false)
    expect(container.querySelector('textarea')?.value).toBe('尚未发送的草稿')
    expect(localStorage.getItem('lumen.sidebarCollapsed')).toBe('true')
    expect(container.querySelectorAll('.sidebar-rail button')).toHaveLength(3)
    expect(lumenApi.startRun).not.toHaveBeenCalled()
  })

  it('opens a selected search result through the session loader and preserves the draft', async () => {
    snapshots.set('架构分享', snapshot('架构分享'))
    await mount()
    await type('尚未发送')
    await click(container.querySelector<HTMLButtonElement>('.sidebar-search-toggle')!)
    await click(container.querySelector<HTMLElement>('.session-search-result')!)
    expect(lumenApi.session).toHaveBeenCalledWith('架构分享')
    expect(new URLSearchParams(window.location.search).get('session')).toBe('架构分享')
    expect(container.querySelector('.session-search-dialog')).toBeNull()
    expect(container.querySelector('textarea')?.value).toBe('尚未发送')
    expect(lumenApi.startRun).not.toHaveBeenCalled()
  })

  it('replaces the mobile sidebar modal with search instead of nesting focus traps', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }))
    await mount()
    await click(container.querySelector<HTMLButtonElement>('.sidebar-toggle')!)
    await click(container.querySelector<HTMLButtonElement>('.sidebar-search-toggle')!)
    expect(container.querySelectorAll('[role="dialog"]')).toHaveLength(1)
    expect(container.querySelector('.session-sidebar')?.classList.contains('is-open')).toBe(false)
    expect(document.activeElement).toBe(container.querySelector('.session-search-header input'))
    await click(button('关闭搜索'))
    expect(container.querySelector('[role="dialog"]')).toBeNull()
    expect(container.querySelector('.agent-workspace')?.hasAttribute('inert')).toBe(false)
    expect(document.activeElement).toBe(container.querySelector('.sidebar-toggle'))
  })

  it('opens a mobile modal without changing the desktop preference and closes it with Escape', async () => {
    vi.stubGlobal('matchMedia', () => ({ matches: true, addEventListener() {}, removeEventListener() {} }))
    localStorage.setItem('lumen.sidebarCollapsed', 'true')
    await mount()
    await click(container.querySelector<HTMLButtonElement>('.sidebar-toggle')!)
    expect(container.querySelector('.session-sidebar')?.getAttribute('role')).toBe('dialog')
    expect(container.querySelector('.agent-workspace')?.hasAttribute('inert')).toBe(true)
    await act(async () => { document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true })) })
    expect(container.querySelector('.session-sidebar')?.hasAttribute('aria-modal')).toBe(false)
    expect(container.querySelector('.agent-workspace')?.hasAttribute('inert')).toBe(false)
    expect(localStorage.getItem('lumen.sidebarCollapsed')).toBe('true')
  })
})

describe('session approval mode interactions', () => {
  it('copies user text, allows unchanged regeneration, and keeps the current session', async () => {
    const source = snapshot('source')
    source.timeline = [
      { id: 'u0', kind: 'user', text: '前文', turn_index: 0, interaction_id: 'run-old' },
      { id: 'a0', kind: 'assistant', text: '前文回答' },
      { id: 'u1', kind: 'user', text: '需要修改', turn_index: 1, interaction_id: 'run-edit', attachments: [
        { artifact_ref: 'sha256:image', kind: 'image', media_type: 'image/png', filename: 'ref.png', byte_size: 12 },
      ] },
      { id: 'a1', kind: 'assistant', text: '应被替换的回答' },
    ]
    snapshots.set('source', source)
    window.history.replaceState({}, '', '/?session=source')
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    vi.mocked(lumenApi.startRun).mockRejectedValueOnce(new Error('网络断开，请重试'))
      .mockImplementationOnce(async () => {
        source.timeline = [
          ...source.timeline.slice(0, 2),
          { id: 'u-new', kind: 'user', text: '需要修改', turn_index: 1, interaction_id: 'replacement-run' },
        ]
        return { runId: 'replacement-run', sessionId: 'source', status: 'running' }
      })
    await mount()
    await type('保留输入框草稿')
    const actions = container.querySelectorAll('.user-message-actions')
    await click(actions[1].querySelector<HTMLButtonElement>('[aria-label="复制消息"]')!)
    expect(writeText).toHaveBeenCalledWith('需要修改')
    await click(actions[1].querySelector<HTMLButtonElement>('[aria-label="编辑消息"]')!)
    expect(button('发送并重新生成').disabled).toBe(false)
    await click(button('发送并重新生成'))
    expect(container.textContent).toContain('网络断开，请重试')
    expect(container.querySelector<HTMLTextAreaElement>('textarea[aria-label="编辑消息"]')?.value).toBe('需要修改')
    expect(container.textContent).toContain('应被替换的回答')
    await click(button('发送并重新生成'))
    expect(lumenApi.forkSession).not.toHaveBeenCalled()
    expect(lumenApi.renameSession).not.toHaveBeenCalled()
    const calls = vi.mocked(lumenApi.startRun).mock.calls
    expect(calls[0]).toEqual(calls[1])
    expect(calls[1]).toEqual(['source', '需要修改', expect.any(String), [
      { artifactRef: 'sha256:image', kind: 'image', mediaType: 'image/png', filename: 'ref.png', byteSize: 12 },
    ], 1])
    expect(window.location.search).toBe('?session=source')
    expect(container.textContent).toContain('前文回答')
    expect(container.textContent).not.toContain('应被替换的回答')
    expect(container.querySelector<HTMLTextAreaElement>('textarea')?.value).toBe('保留输入框草稿')
    expect(source.timeline).toHaveLength(3)
  })

  it('keeps the run active on stop failure and prevents duplicate cancellation while pending', async () => {
    const running = snapshot('running')
    running.activeRunId = 'active-run'
    running.timeline = [{ id: 'u', kind: 'user', text: '正在生成', turn_index: 0, interaction_id: 'active-run' }]
    snapshots.set('running', running)
    window.history.replaceState({}, '', '/?session=running')
    vi.mocked(lumenApi.cancelRun).mockRejectedValueOnce(new Error('停止失败，请重试'))
    await mount()
    await click(button('停止运行'))
    expect(container.textContent).toContain('停止失败，请重试')
    expect(container.querySelector('.timeline [role="alert"]')).toBeNull()
    expect(button('停止运行')).toBeDefined()
    let resolve!: (value: { status: string }) => void
    vi.mocked(lumenApi.cancelRun).mockImplementation(() => new Promise((done) => { resolve = done }))
    await click(button('停止运行'))
    const stopping = button('正在停止')
    expect(stopping.disabled).toBe(true)
    await click(stopping)
    expect(lumenApi.cancelRun).toHaveBeenCalledTimes(2)
    await act(async () => {
      resolve({ status: 'cancelled' })
      vi.mocked(subscribeRun).mock.calls.at(-1)![1]({
        version: 1, sequence: 2, sessionId: 'running', runId: 'active-run',
        type: 'run.cancelled', createdAt: '2026-09-03T00:00:00Z', data: { reason: 'cancelled' },
      })
    })
    expect(button('发送消息')).toBeDefined()
    expect(container.querySelector('.stop-button')).toBeNull()
    expect(container.textContent).toContain('已停止生成。')
  })
  it('restores the recorded turn duration from the API without a browser timer', async () => {
    const recorded = snapshot('session-1')
    recorded.timeline = [
      { id: 'user', kind: 'user', text: 'question', elapsed_seconds: 155.9 },
      { id: 'thought', kind: 'thinking', text: '公开过程' },
      { id: 'answer', kind: 'assistant', text: '最终结果' },
    ]
    snapshots.set('session-1', recorded)
    window.history.replaceState({}, '', '/?session=session-1')
    await mount()
    expect(button('已完成处理 · 2m 35s').getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelector('.timeline-assistant')?.textContent).toContain('最终结果')
    await click(button('已完成处理 · 2m 35s'))
    expect(container.querySelector<HTMLDivElement>('.turn-activity-list')?.hidden).toBe(false)
  })

  it('toggles Plan with /plan without running a task', async () => {
    await mount()
    expect(container.querySelector('[aria-label^="工作方式"]')).toBeNull()
    await type('/plan')
    await click(button('发送消息'))
    expect(lumenApi.updateSessionSettings).toHaveBeenLastCalledWith('session-1', { collaborationMode: 'plan' })
    expect(container.querySelector('.composer-plan-indicator')?.textContent).toContain('Plan')
    expect(lumenApi.startRun).not.toHaveBeenCalled()
    await type('/plan')
    await click(button('发送消息'))
    expect(lumenApi.updateSessionSettings).toHaveBeenLastCalledWith('session-1', { collaborationMode: 'default' })
    expect(container.querySelector('.composer-plan-indicator')).toBeNull()
  })

  it('keeps confirmed modes across workspace refreshes and restores each Session separately', async () => {
    await mount()
    await type('保留我的任务草稿')
    await choose('审批模式：每次确认', '自动执行')
    expect(lumenApi.updateSessionSettings).not.toHaveBeenCalled()
    await click(button('确认开启'))
    expect(button('审批模式：自动执行')).toBeDefined()
    expect(document.querySelector('.auto-confirm-dialog')).toBeNull()
    await choose('模型：model-a', 'model-b') // Refreshes bootstrap, whose defaults stay manual/default.
    expect(button('审批模式：自动执行')).toBeDefined()
    expect(container.querySelector('[aria-label^="工作方式"]')).toBeNull()
    expect(document.querySelector('textarea')?.value).toBe('保留我的任务草稿')
    expect(lumenApi.startRun).not.toHaveBeenCalled()
    await click(button('新建任务'))
    expect(button('审批模式：每次确认')).toBeDefined()
    expect(container.querySelector('.composer-plan-indicator')).toBeNull()
    await click(button('session-1'))
    expect(button('审批模式：自动执行')).toBeDefined()
    expect(container.querySelector('[aria-label^="工作方式"]')).toBeNull()
  })

  it('allows cancellation and prevents duplicate submissions while confirmation is pending', async () => {
    await mount()
    await choose('审批模式：每次确认', '自动执行')
    expect(document.querySelector('.auto-confirm-dialog.session-management-dialog')).not.toBeNull()
    expect(document.querySelector('.auto-confirm-overlay[role="dialog"][aria-modal="true"]')).not.toBeNull()
    await click(button('取消'))
    expect(lumenApi.updateSessionSettings).not.toHaveBeenCalled()
    expect(button('审批模式：每次确认')).toBeDefined()
    await choose('审批模式：每次确认', '自动执行')
    let finish!: (value: Record<string, unknown>) => void
    vi.mocked(lumenApi.updateSessionSettings).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    const confirm = button('确认开启')
    await click(confirm)
    expect(confirm.disabled).toBe(true)
    expect(confirm.textContent).toBe('正在切换…')
    await click(confirm)
    expect(lumenApi.updateSessionSettings).toHaveBeenCalledTimes(1)
    await act(async () => { finish({ status: 'updated' }) })
    expect(button('审批模式：自动执行')).toBeDefined()
  })

  it('keeps the previous mode and shows a retryable error if saving fails', async () => {
    await mount()
    await choose('审批模式：每次确认', '自动执行')
    vi.mocked(lumenApi.updateSessionSettings).mockRejectedValueOnce(new Error('连接失败，请重试'))
    await click(button('确认开启'))
    expect(button('审批模式：每次确认')).toBeDefined()
    expect(document.querySelector('.auto-confirm-error[role="alert"]')?.textContent).toContain('连接失败，请重试')
    expect(button('确认开启').disabled).toBe(false)
    await click(button('确认开启'))
    expect(button('审批模式：自动执行')).toBeDefined()
  })

  it('clears a pending confirmation when leaving a Session', async () => {
    await mount()
    await choose('审批模式：每次确认', '自动执行')
    await click(button('新建任务'))
    expect(document.querySelector('.auto-confirm-dialog')).toBeNull()
    expect(lumenApi.updateSessionSettings).not.toHaveBeenCalled()
    expect(button('审批模式：每次确认')).toBeDefined()
  })

  it('uses the same confirmation flow for /mode auto on a new task', async () => {
    await mount()
    await type('/mode auto')
    await click(button('发送消息'))
    expect(button('确认开启')).toBeDefined()
    await click(button('确认开启'))
    expect(lumenApi.updateSessionSettings).toHaveBeenCalledWith('session-1', { approvalMode: 'auto' })
    expect(button('审批模式：自动执行')).toBeDefined()
    expect(lumenApi.startRun).not.toHaveBeenCalled()
  })

  it('does not apply a late confirmation response to a different task', async () => {
    await mount()
    await choose('审批模式：每次确认', '自动执行')
    let finish!: (value: Record<string, unknown>) => void
    vi.mocked(lumenApi.updateSessionSettings).mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    await click(button('确认开启'))
    await click(button('新建任务'))
    await act(async () => { finish({ status: 'updated' }) })
    expect(button('审批模式：每次确认')).toBeDefined()
    expect(document.querySelector('.auto-confirm-dialog')).toBeNull()
  })

  it('disables approval changes while a run is active', async () => {
    workspace.activeRunId = 'running-task'
    await mount()
    expect(button('审批模式：每次确认').disabled).toBe(true)
    await click(button('审批模式：每次确认'))
    expect(document.querySelector('[role="listbox"]')).toBeNull()
    expect(lumenApi.updateSessionSettings).not.toHaveBeenCalled()
  })
})
