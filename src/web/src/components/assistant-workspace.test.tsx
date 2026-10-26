// @vitest-environment jsdom
import { act } from 'react'
import { createRoot, type Root } from 'react-dom/client'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { lumenApi } from '@/lib/api/client'
import type { AssistantResource, AssistantState } from '@/lib/api/types'
import { AssistantWorkspace } from './assistant-workspace'
import { PersonalPreferences } from './personal-preferences'

Object.assign(globalThis, { IS_REACT_ACT_ENVIRONMENT: true })

vi.mock('@/lib/api/client', () => ({
  resourceFileUrl: (id: string, revision: number) => `/api/v1/assistant/resources/${id}/content?revision=${revision}`,
  lumenApi: {
    assistant: vi.fn(),
    assistantResources: vi.fn(),
    assistantFeed: vi.fn(),
    assistantResource: vi.fn(),
    assistantOccurrence: vi.fn(),
    assistantGoal: vi.fn(),
    controlGoal: vi.fn(),
    automation: vi.fn(),
    editAssistant: vi.fn(),
    cancelRun: vi.fn(),
    preferences: vi.fn(),
    editPreference: vi.fn(),
    readResourceFile: vi.fn(),
    importResourceFile: vi.fn(),
  },
}))

let root: Root
let container: HTMLDivElement
let state: AssistantState
let resource: AssistantResource

beforeEach(() => {
  vi.clearAllMocks()
  vi.stubGlobal('ResizeObserver', class {observe() {} disconnect() {}})
  state = {
    owner: true,
    status: 'running',
    workspace_source_available: true,
    definitions: [],
    occurrences: [],
    feed: [],
    resources: [],
    goals: [],
    ideas: [],
    collections: [],
    research_available: false,
  }
  resource = {
    id: 'note-one',
    title: 'A saved reading note',
    kind: 'note',
    artifact_ref: `sha256:${'a'.repeat(64)}`,
    created_at: '2026-10-20T00:00:00Z',
    saved: false,
    revision: 1,
    status: 'active',
    published: true,
  }
  vi.mocked(lumenApi.assistant).mockImplementation(async () =>
    structuredClone(state),
  )
  vi.mocked(lumenApi.assistantResources).mockImplementation(async () => ({
    items: [structuredClone(resource)],
    next_cursor: null,
  }))
  vi.mocked(lumenApi.assistantFeed).mockResolvedValue({
    items: [],
    next_cursor: null,
  })
  vi.mocked(lumenApi.editAssistant).mockImplementation(async (value) => {
    if (value.action === 'save') resource.saved = Boolean(value.value)
    return { status: 'saved' }
  })
  vi.mocked(lumenApi.automation).mockResolvedValue({ id: 'automation-one' })
  container = document.createElement('div')
  document.body.append(container)
  root = createRoot(container)
})

afterEach(async () => {
  await act(async () => root.unmount())
  container.remove()
  vi.unstubAllGlobals()
})

async function settle() {
  await act(async () => {
    await new Promise((resolve) => window.setTimeout(resolve, 220))
  })
}

async function click(label: string) {
  const button = [...document.querySelectorAll('button')].find(
    (item) =>
      item.textContent?.trim() === label ||
      item.getAttribute('aria-label') === label,
  )
  if (!button) throw new Error(`Missing control ${label}`)
  await act(async () => button.click())
  return button
}

async function fill(label: string, value: string) {
  const wrapper = [...container.querySelectorAll('label')].find((item) =>
    item.textContent?.startsWith(label),
  )
  const field = (wrapper?.querySelector('input,textarea') ??
    [...container.querySelectorAll('input[aria-label]')].find(input => input.getAttribute('aria-label') === label)) as
    | HTMLInputElement
    | HTMLTextAreaElement
    | null
  if (!field) throw new Error(`Missing field ${label}`)
  await act(async () => {
    const prototype =
      field.tagName === 'TEXTAREA'
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype
    Object.getOwnPropertyDescriptor(prototype, 'value')!.set!.call(field, value)
    field.dispatchEvent(new Event('input', { bubbles: true }))
  })
}

it('keeps deleted goals out of the feed aside while retaining them in the goal recycle bin', async () => {
  state.goals = [
    {id: 'deleted', title: 'Deleted goal', intent: '', revision: 2, status: 'active', milestones: [], automation_ids: [],
      deleted_at: '2026-10-21T00:00:00Z'},
    {id: 'active', title: 'Current goal', intent: '', revision: 1, status: 'active', milestones: [], automation_ids: []},
  ]
  await act(async () => root.render(<AssistantWorkspace page="feed" initialIntent=""
    onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await settle()
  expect(container.querySelector('.assistant-feed-aside')?.textContent).toContain('Current goal')
  expect(container.querySelector('.assistant-feed-aside')?.textContent).not.toContain('Deleted goal')
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent=""
    onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await click('回收站')
  expect(container.textContent).toContain('Deleted goal')
})

it('keeps feed context optional on small screens and expands it on desktop', async () => {
  const media = {matches: false, addEventListener: vi.fn(), removeEventListener: vi.fn()}
  vi.stubGlobal('matchMedia', () => media)
  await act(async () => root.render(<AssistantWorkspace page="feed" initialIntent=""
    onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await settle()
  const details = container.querySelector<HTMLDetailsElement>('.assistant-feed-aside details')!
  expect(details.open).toBe(false)
  await act(async () => {
    media.matches = true
    media.addEventListener.mock.calls[0][1]()
  })
  expect(details.open).toBe(true)
})

it('shows an Idea proposal before explicitly starting a scoped task', async () => {
  const discuss = vi.fn()
  const start = vi.fn().mockResolvedValue(undefined)
  state.ideas = [
    {
      id: 'review',
      title: 'A deliberate idea',
      reason: 'A rule-based suggestion',
      intent: 'Review the selected project files with me',
      category: 'work',
      dismissed: false,
      context_refs: [],
      source: 'template', source_label: '通用起点', outcome: 'A review file', steps: ['Read', 'Review'],
      input_hint: 'Select files', destination: 'task',
    },
  ]
  await act(async () =>
    root.render(
      <AssistantWorkspace
        page="ideas"
        initialIntent=""
        onDiscuss={discuss}
        onStartIdea={start}
        onOpenSession={vi.fn()}
      />,
    ),
  )
  await click('查看方案')
  expect(start).not.toHaveBeenCalled()
  expect(container.querySelector<HTMLButtonElement>('button[type="submit"]')?.disabled).toBe(true)
  await fill('先确定任务范围', 'README.md, last week')
  await click('开始任务')
  expect(start).toHaveBeenCalledWith(state.ideas[0], expect.stringContaining('README.md, last week'))
  expect(discuss).not.toHaveBeenCalled()
  expect(lumenApi.automation).not.toHaveBeenCalled()
})

it('keeps the delegation draft and retry identity when saving fails', async () => {
  vi.mocked(lumenApi.automation).mockRejectedValueOnce(
    new Error('Connection interrupted'),
  )
  await act(async () =>
    root.render(
      <AssistantWorkspace
        page="automations"
        initialIntent="Every Friday, review my project"
        onDiscuss={vi.fn()}
        onOpenSession={vi.fn()}
      />,
    ),
  )
  await fill('名称', 'Weekly review')
  await click('保存草稿')
  const dialog = container.querySelector('[role="dialog"]')
  expect(dialog?.textContent).toContain('Connection interrupted')
  expect((dialog?.querySelector('textarea') as HTMLTextAreaElement).value).toBe(
    'Every Friday, review my project',
  )
  const first = vi.mocked(lumenApi.automation).mock.calls[0][0]
  expect(first.action).toBe('create')
  expect(first.spec?.paths).toEqual([])
  expect(first.spec?.profile).toBe('research_readonly')
  await click('保存草稿')
  expect(vi.mocked(lumenApi.automation).mock.calls[1][0].request_id).toBe(
    first.request_id,
  )
  expect(container.querySelector('[role="dialog"]')).toBeNull()
})

it('queries the Library on the server and preserves a bookmark control after saving', async () => {
  await act(async () =>
    root.render(
      <AssistantWorkspace
        page="library"
        initialIntent=""
        onDiscuss={vi.fn()}
        onOpenSession={vi.fn()}
      />,
    ),
  )
  await settle()
  expect(lumenApi.assistantResources).toHaveBeenCalledWith('', 'all')
  await click('网格视图')
  const bookmark = container.querySelector(
    'button[aria-label="收藏 A saved reading note"]',
  ) as HTMLButtonElement
  bookmark.focus()
  await act(async () => bookmark.click())
  await settle()
  expect(lumenApi.editAssistant).toHaveBeenCalledWith({
    action: 'save',
    id: 'note-one',
    value: true,
  })
  expect(
    container
      .querySelector('button[aria-label="收藏 A saved reading note"]')
      ?.getAttribute('aria-pressed'),
  ).toBe('true')
  expect(document.activeElement).toBe(bookmark)
  const input = container.querySelector(
    '.library-search input',
  ) as HTMLInputElement
  await act(async () => {
    Object.getOwnPropertyDescriptor(
      HTMLInputElement.prototype,
      'value',
    )!.set!.call(input, 'older note')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await settle()
  expect(lumenApi.assistantResources).toHaveBeenLastCalledWith(
    'older note',
    'all',
  )
})

it('does not present an empty result when a Library query fails', async () => {
  vi.mocked(lumenApi.assistantResources).mockRejectedValue(
    new Error('Storage unavailable'),
  )
  await act(async () =>
    root.render(
      <AssistantWorkspace
        page="library"
        initialIntent=""
        onDiscuss={vi.fn()}
        onOpenSession={vi.fn()}
      />,
    ),
  )
  await settle()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    'Storage unavailable',
  )
  expect(container.querySelector('.assistant-empty')).toBeNull()
})

it('shows result and stop errors inside the active execution dialog', async () => {
  const occurrence: AssistantState['occurrences'][number] = {
    id: 'execution-one',
    automation_id: 'automation-one',
    definition_revision: 1,
    kind: 'manual',
    edition: 'manual:one',
    session_id: 'session-one',
    interaction_id: 'run-one',
    phase: 'running',
    result: 'complete',
    created_at: '2026-10-20T00:00:00Z',
    artifact_ref: resource.artifact_ref,
    receipts: [],
    detail: 'A completed result',
    preference_snapshot: [],
    session_available: true,
  }
  state.occurrences = [occurrence]
  vi.mocked(lumenApi.assistantOccurrence).mockResolvedValue(occurrence)
  vi.mocked(lumenApi.assistantFeed).mockResolvedValue({
    items: [
      {
        id: occurrence.id,
        occurrence_id: occurrence.id,
        title: 'A review',
        detail: '',
        sequence: 1,
        result: 'complete',
        created_at: occurrence.created_at,
        read: false,
        saved: false,
      },
    ],
    next_cursor: null,
  })
  vi.mocked(lumenApi.assistantResource).mockRejectedValue(
    new Error('Result unavailable'),
  )
  vi.mocked(lumenApi.cancelRun).mockRejectedValue(new Error('Stop unavailable'))
  await act(async () =>
    root.render(
      <AssistantWorkspace
        page="feed"
        initialIntent=""
        onDiscuss={vi.fn()}
        onOpenSession={vi.fn()}
      />,
    ),
  )
  await settle()
  await click('执行详情')
  await click('查看结果')
  expect(
    container.querySelector('[role="dialog"] [role="alert"]')?.textContent,
  ).toContain('Result unavailable')
  await click('停止本次执行')
  expect(
    container.querySelector('[role="dialog"] [role="alert"]')?.textContent,
  ).toContain('Stop unavailable')
})

it('ends a failed preference read and supports repeated failure then successful retry', async () => {
  vi.mocked(lumenApi.preferences)
    .mockRejectedValueOnce(new Error('First read failed'))
    .mockRejectedValueOnce(new Error('Second read failed'))
    .mockResolvedValue({ available: true, records: [] })
  await act(async () => root.render(<PersonalPreferences />))
  expect(container.querySelector('[role="status"]')).toBeNull()
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    'First read failed',
  )
  await click('重新读取')
  expect(container.querySelector('[role="alert"]')?.textContent).toContain(
    'Second read failed',
  )
  await click('重新读取')
  expect(container.querySelector('[role="alert"]')).toBeNull()
  expect(container.textContent).toContain('还没有保存的偏好')
})

it('locks the preference draft until saving and refreshing finish', async () => {
  vi.mocked(lumenApi.preferences).mockResolvedValue({ available: true, records: [] })
  let finish!: () => void
  vi.mocked(lumenApi.editPreference).mockImplementation(() => new Promise(resolve => {
    finish = () => resolve({ status: 'saved' })
  }))
  await act(async () => root.render(<PersonalPreferences />))
  const input = container.querySelector<HTMLTextAreaElement>('textarea')!
  await act(async () => {
    Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, 'value')!.set!.call(input, 'Use Chinese')
    input.dispatchEvent(new Event('input', { bubbles: true }))
  })
  await click('保存偏好')
  expect(input.disabled).toBe(true)
  expect(container.querySelector<HTMLSelectElement>('select')!.disabled).toBe(true)
  let refreshed!: () => void
  vi.mocked(lumenApi.preferences).mockImplementation(() => new Promise(resolve => {
    refreshed = () => resolve({ available: true, records: [] })
  }))
  await act(async () => finish())
  expect(input.disabled).toBe(true)
  await act(async () => refreshed())
  expect(input.disabled).toBe(false)
  expect(input.value).toBe('')
})

it('reuses a control request identity after a lost response and renews it after success', async () => {
  state.definitions = [
    {
      id: 'automation-one',
      revision: 1,
      enabled: false,
      created_at: '2026-10-20T00:00:00Z',
      updated_at: '2026-10-20T00:00:00Z',
      spec: {
        name: 'A review',
        intent: 'Summarize',
        paths: ['README.md'],
        notify: true,
        request_limit: 6,
        timeout_seconds: 180,
        context_refs: [],
        profile: 'workspace_snapshot',
      },
    },
  ]
  vi.mocked(lumenApi.automation).mockRejectedValueOnce(
    new Error('Response lost'),
  )
  await act(async () =>
    root.render(
      <AssistantWorkspace
        page="automations"
        initialIntent=""
        onDiscuss={vi.fn()}
        onOpenSession={vi.fn()}
      />,
    ),
  )
  await click('试跑')
  const first = vi.mocked(lumenApi.automation).mock.calls[0][0]
  await click('试跑')
  expect(vi.mocked(lumenApi.automation).mock.calls[1][0]).toEqual(first)
  await click('试跑')
  expect(vi.mocked(lumenApi.automation).mock.calls[2][0].request_id).not.toBe(
    first.request_id,
  )
})

it('renames, confirms deletion, retries errors, and restores resources through the recycle bin', async () => {
  vi.mocked(lumenApi.assistantResources).mockImplementation(async (_query, category) => ({
    items: Boolean(resource.deleted_at) === (category === 'trash') ? [structuredClone(resource)] : [], next_cursor: null,
  }))
  vi.mocked(lumenApi.editAssistant).mockImplementation(async edit => {
    if (edit.action === 'rename_resource') resource = {...resource, title: edit.title!, revision: 2}
    if (edit.action === 'trash_resource') resource = {...resource, deleted_at: '2026-10-21T00:00:00Z', revision: 3}
    if (edit.action === 'restore_resource') resource = {...resource, deleted_at: null, revision: 4}
    return {status: 'saved'}
  })
  await act(async () => root.render(<AssistantWorkspace page="library" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await settle()
  await click('网格视图')
  await click(`管理资料 ${resource.title}`)
  await click('重命名')
  await fill('名称', 'Renamed note')
  await click('保存名称')
  await settle()
  expect(lumenApi.editAssistant).toHaveBeenCalledWith(expect.objectContaining({action: 'rename_resource', expected_revision: 1}))
  await click('管理资料 Renamed note')
  await click('删除资料')
  expect(lumenApi.editAssistant).toHaveBeenCalledTimes(1)
  vi.mocked(lumenApi.editAssistant).mockRejectedValueOnce(new Error('暂时失败'))
  await click('确认删除')
  expect(container.textContent).toContain('暂时失败')
  await click('确认删除')
  await settle()
  expect(container.querySelector('.library-resource-open')).toBeNull()
  const filter = container.querySelector<HTMLSelectElement>('select[aria-label="资源类型"]')!
  await act(async () => {filter.value = 'trash'; filter.dispatchEvent(new Event('change', {bubbles: true}))})
  await settle()
  await click('恢复 Renamed note')
  await settle()
  expect(lumenApi.editAssistant).toHaveBeenLastCalledWith({action: 'restore_resource', id: resource.id, expected_revision: 3})
})

it('manages goal status and deletion with a recoverable list', async () => {
  state.goals = [{id: 'goal', title: 'Research goal', intent: 'Read sources', status: 'active', revision: 1,
    milestones: [], automation_ids: []}]
  vi.mocked(lumenApi.editAssistant).mockImplementation(async edit => {
    if (edit.action === 'goal') state.goals[0] = {...state.goals[0], ...edit.goal, revision: 2}
    if (edit.action === 'trash_goal') state.goals[0] = {...state.goals[0], deleted_at: '2026-10-21T00:00:00Z', revision: 3}
    if (edit.action === 'restore_goal') state.goals[0] = {...state.goals[0], deleted_at: null, revision: 4}
    return {status: 'saved'}
  })
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await click('管理目标 Research goal')
  await click('标记为已完成')
  expect(container.textContent).toContain('已完成')
  await click('管理目标 Research goal')
  await click('删除目标')
  await click('确认删除')
  expect(container.querySelector('.goal-row-heading')).toBeNull()
  await click('回收站')
  await click('恢复目标')
  expect(lumenApi.editAssistant).toHaveBeenLastCalledWith({action: 'restore_goal', id: 'goal', expected_revision: 3})
})

it('renames and deletes a collection without deleting its resources', async () => {
  state.collections = [{id: 'set', title: 'Reading', revision: 1}]
  vi.mocked(lumenApi.editAssistant).mockImplementation(async edit => {
    if (edit.action === 'collection') state.collections[0] = {...state.collections[0], title: edit.title!, revision: 2}
    if (edit.action === 'delete_collection') state.collections = []
    return {status: 'saved'}
  })
  await act(async () => root.render(<AssistantWorkspace page="library" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await settle()
  const filter = container.querySelector<HTMLSelectElement>('select[aria-label="资源类型"]')!
  await act(async () => {filter.value = 'collection:set'; filter.dispatchEvent(new Event('change', {bubbles: true}))})
  await settle()
  await click('管理集合 Reading')
  await click('重命名')
  await fill('名称', 'New reading')
  await click('保存名称')
  await settle()
  await click('管理集合 New reading')
  await click('删除集合')
  expect(container.textContent).toContain('里面的资料仍保留')
  await click('确认删除')
  await settle()
  await click('网格视图')
  expect(container.querySelector('.library-resource-open')).not.toBeNull()
  expect(lumenApi.editAssistant).toHaveBeenLastCalledWith({action: 'delete_collection', id: 'set', expected_revision: 2})
})

it('imports actual files and rejects files over the upload limit', async () => {
  vi.mocked(lumenApi.importResourceFile).mockResolvedValue({...resource, kind: 'file', file_path: 'image.png', file_type: 'image'})
  await act(async () => root.render(<AssistantWorkspace page="library" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  const input = container.querySelector<HTMLInputElement>('input[type="file"]')!
  const file = new File(['image'], 'image.png', {type: 'image/png'})
  await act(async () => {Object.defineProperty(input, 'files', {value: [file], configurable: true}); input.dispatchEvent(new Event('change', {bubbles: true}))})
  expect(lumenApi.importResourceFile).toHaveBeenCalledWith(file, expect.any(String))
  Object.defineProperty(file, 'size', {value: 21 * 1024 * 1024})
  await act(async () => input.dispatchEvent(new Event('change', {bubbles: true})))
  expect(container.textContent).toContain('20 MiB')
  expect(lumenApi.importResourceFile).toHaveBeenCalledTimes(1)
})

it('creates a goal with only intent and explicitly starts its planning callback', async () => {
  const discuss = vi.fn()
  const navigate = vi.fn()
  const plan = vi.fn().mockResolvedValue(undefined)
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="" onDiscuss={discuss} onOpenSession={vi.fn()} onNavigate={navigate} onPlanGoal={plan} />))
  await click('新建目标')
  expect(container.querySelector('[role="dialog"] details')).toBeNull()
  expect(container.querySelector('[role="dialog"] input[type="checkbox"]')).toBeNull()
  expect(container.textContent).not.toContain('关联自动化')
  await fill('想达成什么', '每天了解 AI 的进展')
  await click('创建并规划')
  const saved = vi.mocked(lumenApi.editAssistant).mock.calls[0][0]
  expect(saved.goal?.milestones).toEqual([])
  expect(saved.goal?.automation_ids).toEqual([])
  expect(navigate).toHaveBeenCalledWith('goals', saved.id)
  expect(plan).toHaveBeenCalledWith(saved.id, saved.id)
  expect(discuss).not.toHaveBeenCalled()
  expect(lumenApi.automation).not.toHaveBeenCalled()
})

it('retains the original conversation when creating a goal from its intent', async () => {
  const created = vi.fn()
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="Keep existing constraints"
    sourceSessionId="original-session" onGoalCreated={created} onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await click('创建并规划')
  expect(lumenApi.editAssistant).toHaveBeenCalledWith(expect.objectContaining({
    source_session_id: 'original-session', goal: expect.objectContaining({title: 'Keep existing constraints'}),
  }))
  expect(created).toHaveBeenCalledOnce()
  expect(lumenApi.controlGoal).not.toHaveBeenCalled()
})

it('retries planning with the saved goal and the same identity without creating another goal', async () => {
  const plan = vi.fn().mockRejectedValueOnce(new Error('Response lost')).mockResolvedValue(undefined)
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="A useful result"
    onDiscuss={vi.fn()} onOpenSession={vi.fn()} onPlanGoal={plan} />))
  await click('创建并规划')
  expect(container.textContent).toContain('目标已保存，规划尚未开始')
  expect(container.textContent).toContain('Response lost')
  expect(container.querySelector('[role="dialog"]')).toBeNull()
  await click('重试规划')
  expect(lumenApi.editAssistant).toHaveBeenCalledOnce()
  expect(plan.mock.calls[0]).toEqual(plan.mock.calls[1])
  expect(container.textContent).not.toContain('目标已保存，规划尚未开始')
})

it('requires success criteria for explicit goal execution and exposes pause separately from completion', async () => {
  state.goals = [{id: 'goal', title: 'Verify', intent: '', revision: 1, status: 'active', milestones: [], automation_ids: []}]
  const detail = () => ({goal: structuredClone(state.goals[0]), resources: [], context_resources: [], definitions: [], occurrences: []})
  vi.mocked(lumenApi.assistantGoal).mockImplementation(async () => detail())
  vi.mocked(lumenApi.controlGoal).mockImplementation(async command => {
    state.goals[0] = {...state.goals[0], revision: 2, execution: {id: command.request_id,
      criteria: command.criteria, status: command.action === 'pause' ? 'paused' : 'active', turns: 1,
      turn_limit: command.turn_limit, detail: 'Current execution', evidence: []}}
    return detail()
  })
  await act(async () => root.render(<AssistantWorkspace page="goals" objectId="goal" initialIntent=""
    onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  expect(container.textContent).toContain('未完成')
  expect(container.textContent).toContain('尚未启动')
  await click('启动目标推进')
  expect((await click('启动推进')).disabled).toBe(true)
  await fill('怎样才算达成', 'Build passes and the result is inspectable')
  await fill('推进轮数上限（可选）', '2')
  await click('启动推进')
  expect(lumenApi.controlGoal).toHaveBeenCalledWith(expect.objectContaining({action: 'start',
    criteria: 'Build passes and the result is inspectable', turn_limit: 2, expected_revision: 1}))
  expect(container.textContent).toContain('正在推进')
  expect(container.textContent).toContain('未完成')
  await click('暂停推进')
  expect(lumenApi.controlGoal).toHaveBeenLastCalledWith(expect.objectContaining({action: 'pause'}))
  expect(container.textContent).toContain('已暂停')
  expect(lumenApi.cancelRun).not.toHaveBeenCalled()
})

it.each(['pause', 'keep'] as const)('requires an explicit %s choice for enabled follow-ups at goal completion', async policy => {
  state.goals = [{id: 'goal', title: 'Outcome', intent: '', revision: 1, status: 'active', milestones: [], automation_ids: ['follow']}]
  state.definitions = [{id: 'follow', revision: 1, enabled: true, created_at: '', updated_at: '',
    spec: {name: 'Weekly review', intent: 'Review', paths: ['README.md'], profile: 'workspace_snapshot',
      schedule: {kind: 'weekly', timezone: 'Asia/Shanghai', weekday: 4, hour: 8, minute: 30},
      request_limit: 6, timeout_seconds: 180, notify: true, context_refs: []}}]
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await click('管理目标 Outcome')
  await click('标记为已完成')
  expect(lumenApi.editAssistant).not.toHaveBeenCalled()
  expect(container.querySelector('[role="dialog"]')?.textContent).toContain('Weekly review')
  await click(policy === 'pause' ? '完成并暂停跟进' : '完成并保留跟进')
  expect(lumenApi.editAssistant).toHaveBeenCalledWith(expect.objectContaining({follow_up_policy: policy,
    expected_revision: 1, goal: expect.objectContaining({status: 'completed', automation_ids: ['follow']})}))
})

it('keeps milestone identities, completion and existing follow-ups when editing a goal', async () => {
  state.goals = [{id: 'goal', title: 'Goal', intent: '', revision: 3, status: 'active', automation_ids: ['existing'],
    milestones: [{id: 'first', title: 'First', done: false}, {id: 'second', title: 'Second', done: true}]}]
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="" onDiscuss={vi.fn()}
    onOpenSession={vi.fn()} onNavigate={vi.fn()} />))
  await click('管理目标 Goal')
  await click('编辑目标')
  await click('删除里程碑 1')
  await fill('里程碑 1', 'Revised second')
  await click('添加里程碑')
  await fill('里程碑 2', 'Next')
  await click('关闭')
  expect(container.textContent).toContain('还有未保存的修改')
  await click('继续编辑')
  await click('保存修改')
  const saved = vi.mocked(lumenApi.editAssistant).mock.calls[0][0]
  expect(saved.expected_revision).toBe(3)
  expect(saved.goal?.automation_ids).toEqual(['existing'])
  expect(saved.goal?.milestones[0]).toEqual({id: 'second', title: 'Revised second', done: true})
  expect(saved.goal?.milestones[1]).toMatchObject({title: 'Next', done: false})
  expect(['first', 'second']).not.toContain(saved.goal?.milestones[1].id)
})

it('opens goal planning as a draft and keeps follow-ups outside the core goal workflow', async () => {
  const goal = {id: 'goal', title: 'A clear outcome', intent: 'Understand what success means', revision: 1,
    status: 'active' as const, automation_ids: [], milestones: []}
  state.goals = [goal]
  vi.mocked(lumenApi.assistantGoal).mockResolvedValue({goal, resources: [], context_resources: [], definitions: [], occurrences: []})
  const discuss = vi.fn()
  await act(async () => root.render(<AssistantWorkspace page="goals" objectId="goal" initialIntent=""
    onDiscuss={discuss} onOpenSession={vi.fn()} />))
  expect(container.querySelectorAll('[aria-label="目标里程碑"]')).toHaveLength(1)
  expect(container.querySelector('.assistant-goals > article')).toBeNull()
  expect(container.querySelector('details.goal-follow-ups')?.hasAttribute('open')).toBe(false)
  expect(container.textContent).not.toContain('现在整理一次')
  expect(container.textContent).not.toContain('关联自动化')
  await click('开始规划')
  expect(discuss).toHaveBeenCalledWith({intent: '', context: {kind: 'goal', id: 'goal'}})
  expect(lumenApi.automation).not.toHaveBeenCalled()
})

it('refreshes goal results and milestones while retaining an open editor draft', async () => {
  vi.useFakeTimers()
  try {
    const goal = {id: 'goal', title: 'Original goal', intent: '', revision: 1, status: 'active' as const,
      automation_ids: [], milestones: []}
    state.goals = [goal]
    const detail = {goal, resources: [] as AssistantResource[], context_resources: [], definitions: [], occurrences: []}
    vi.mocked(lumenApi.assistantGoal).mockImplementation(async () => structuredClone(detail))
    await act(async () => root.render(<AssistantWorkspace page="goals" objectId="goal" initialIntent=""
      onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
    await click('编辑目标')
    await fill('想达成什么', 'Keep this draft')
    detail.resources = [{...resource, title: 'New result.md', file_path: 'New result.md'}]
    await act(async () => vi.advanceTimersByTimeAsync(3000))
    expect(container.querySelector('[aria-label="目标成果"]')?.textContent).toContain('New result.md')
    expect(container.querySelector<HTMLInputElement>('[role="dialog"] input')?.value).toBe('Keep this draft')
  } finally { vi.useRealTimers() }
})

it('rejects blank goal titles and prevents closing during a pending save', async () => {
  let finish!: () => void
  vi.mocked(lumenApi.editAssistant).mockImplementationOnce(() => new Promise(resolve => {
    finish = () => resolve({status: 'saved'})
  }))
  await act(async () => root.render(<AssistantWorkspace page="goals" initialIntent="" onDiscuss={vi.fn()}
    onOpenSession={vi.fn()} onNavigate={vi.fn()} />))
  await click('新建目标')
  await fill('想达成什么', '   ')
  expect((await click('创建并规划')).disabled).toBe(true)
  expect(lumenApi.editAssistant).not.toHaveBeenCalled()
  await fill('想达成什么', ' A goal ')
  await click('创建并规划')
  expect((await click('关闭')).disabled).toBe(true)
  expect(container.textContent).toContain('正在保存')
  await act(async () => finish())
  expect(container.querySelector('[role="dialog"]')).toBeNull()
  expect(vi.mocked(lumenApi.editAssistant).mock.calls[0][0].goal?.title).toBe('A goal')
})

it('ignores late goal responses after returning to the goal list', async () => {
  const goal = {id: 'old', title: 'Old detail', intent: '', revision: 1, status: 'active' as const,
    automation_ids: [], milestones: []}
  let finish!: () => void
  vi.mocked(lumenApi.assistantGoal).mockImplementation(() => new Promise(resolve => {
    finish = () => resolve({goal, resources: [], context_resources: [], definitions: [], occurrences: []})
  }))
  const render = (objectId?: string) => root.render(<AssistantWorkspace page="goals" objectId={objectId}
    initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />)
  await act(async () => render('old'))
  expect(container.textContent).toContain('正在读取目标')
  await act(async () => render())
  await act(async () => finish())
  expect(container.textContent).not.toContain('Old detail')
  expect(container.textContent).not.toContain('正在读取目标')
  expect(container.textContent).toContain('从一个明确的目标开始')
})

it('discusses a resource by identity and version without copying its body into the prompt', async () => {
  const discuss = vi.fn()
  vi.mocked(lumenApi.assistantResource).mockResolvedValue({resource, content: 'Untrusted source body', historical: false})
  await act(async () => root.render(<AssistantWorkspace page="library" initialIntent="" onDiscuss={discuss} onOpenSession={vi.fn()} />))
  await settle()
  await click('网格视图')
  await click(`打开 ${resource.title}`)
  await click('继续聊聊')
  expect(discuss).toHaveBeenCalledWith({intent: '', context: {kind: 'resource', id: resource.id, revision: 1}})
})

it('opens and discusses the output revision displayed in goal details', async () => {
  const goal = {id: 'goal', title: 'Goal', intent: '', revision: 1, status: 'active' as const,
    automation_ids: [], milestones: []}
  vi.mocked(lumenApi.assistantGoal).mockResolvedValue({goal, resources: [resource], context_resources: [], definitions: [], occurrences: []})
  vi.mocked(lumenApi.assistantResource).mockResolvedValue({resource, content: 'Original result', historical: false})
  const discuss = vi.fn()
  await act(async () => root.render(<AssistantWorkspace page="goals" objectId="goal" initialIntent=""
    onDiscuss={discuss} onOpenSession={vi.fn()} />))
  await act(async () => container.querySelector<HTMLButtonElement>('.goal-resource-link')!.click())
  expect(lumenApi.assistantResource).toHaveBeenLastCalledWith(resource.id, false, resource.revision)
  await click('继续聊聊')
  expect(discuss).toHaveBeenCalledWith({intent: '', context: {kind: 'resource', id: resource.id, revision: resource.revision}})
})

it('keeps a quiet follow-up quiet when its draft is saved from the UI', async () => {
  state.definitions = [{id: 'quiet', revision: 1, enabled: false,
    created_at: resource.created_at, updated_at: resource.created_at,
    spec: {name: 'Quiet review', intent: 'Review', paths: ['README.md'], notify: false,
      request_limit: 6, timeout_seconds: 180, context_refs: [], profile: 'workspace_snapshot'}}]
  await act(async () => root.render(<AssistantWorkspace page="automations" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await click('编辑')
  await click('保存草稿')
  expect(vi.mocked(lumenApi.automation).mock.calls[0][0].spec?.notify).toBe(false)
})

it('protects unsaved page content when closing the editor', async () => {
  await act(async () => root.render(<AssistantWorkspace page="library" initialIntent="" onDiscuss={vi.fn()} onOpenSession={vi.fn()} />))
  await click('新建页面')
  await fill('标题', 'A draft to keep')
  await click('关闭')
  expect(container.textContent).toContain('还有未保存的修改')
  await click('继续编辑')
  expect((container.querySelector('[role="dialog"] input') as HTMLInputElement).value).toBe('A draft to keep')
  expect(lumenApi.editAssistant).not.toHaveBeenCalled()
})

it('filters actual file types, switches list and grid views, and opens the stored HTML preview', async () => {
  resource = {...resource, title: 'site.html', kind: 'file', file_type: 'site', file_path: 'outputs/site.html',
    byte_size: 300, session_id: 'original-chat'}
  vi.stubGlobal('ResizeObserver', class {observe() {} disconnect() {}})
  Object.defineProperty(URL, 'createObjectURL', {configurable: true, value: vi.fn(() => 'blob:site')})
  Object.defineProperty(URL, 'revokeObjectURL', {configurable: true, value: vi.fn()})
  vi.mocked(lumenApi.readResourceFile).mockResolvedValue(new Blob(['<h1>Saved webpage</h1><script>unsafe()</script>']))
  vi.mocked(lumenApi.assistantResource).mockResolvedValue({resource, content: '', historical: false})
  const openSession = vi.fn()
  await act(async () => root.render(<AssistantWorkspace page="library" initialIntent="" onDiscuss={vi.fn()} onOpenSession={openSession} />))
  await settle()
  await click('列表视图')
  expect(container.querySelector('.library-columns')?.textContent).toContain('最近更新')
  await click('网页')
  await settle()
  expect(lumenApi.assistantResources).toHaveBeenLastCalledWith('', 'site')
  await click('网格视图')
  expect(container.querySelector('.assistant-resource-list.is-grid')).not.toBeNull()
  expect(container.querySelector('.library-cover .library-type-cover')?.textContent).toContain('site.html')
  expect(container.querySelector('.library-cover .library-thumbnail.type-site')).not.toBeNull()
  expect(container.querySelector('.shader-frame')).toBeNull()
  expect(container.textContent).not.toContain('ASHEN PRESS')
  await click('列表视图')
  await click('对话')
  expect(openSession).toHaveBeenCalledWith('original-chat')
  await click('打开 site.html')
  expect(lumenApi.readResourceFile).toHaveBeenCalledWith(resource.id, 1, expect.any(AbortSignal))
  const preview = container.querySelector<HTMLIFrameElement>('iframe[title="HTML 静态预览"]')
  expect(preview?.getAttribute('sandbox')).toBe('')
  expect(preview?.srcdoc).toContain('Saved webpage')
  expect(preview?.srcdoc).not.toContain('<script>')
  await click('资料详情')
  expect(container.textContent).toContain('outputs/site.html')
  expect(container.textContent).toContain('关联目标')
})
