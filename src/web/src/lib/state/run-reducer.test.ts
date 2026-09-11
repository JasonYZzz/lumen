import { describe, expect, it } from 'vitest'
import { initialRunState, runReducer } from './run-reducer'
import type { EventEnvelope } from '@/lib/api/types'

function event(type: EventEnvelope['type'], data: Record<string, unknown>, sequence = 1): EventEnvelope {
  return {
    version: 1,
    sequence,
    sessionId: 'session-1',
    runId: 'run-1',
    type,
    createdAt: '2026-07-27T00:00:00Z',
    data,
  }
}

describe('runReducer', () => {
  it('preserves structured clarification choices and clears them only when a new run is accepted', () => {
    const requested = runReducer(initialRunState, { type: 'event', event: event('clarification.requested', {
      question_id: 'q1', question: '请选择', choices: ['全局安装', '取消'],
    }) })
    expect(requested.pendingClarification).toMatchObject({ id: 'q1', question: '请选择', choices: ['全局安装', '取消'] })
    const waiting = runReducer(requested, { type: 'event', event: event('run.waiting_for_user', {}) })
    expect(waiting.pendingClarification).toEqual(requested.pendingClarification)
    const failed = runReducer(waiting, { type: 'local-error', message: 'network error' })
    expect(failed.pendingClarification).toEqual(requested.pendingClarification)
    const accepted = runReducer(waiting, { type: 'run-registered', runId: 'answer' })
    expect(accepted.pendingClarification).toBeNull()
  })
  it('replaces an active snapshot suffix when replaying the same run from its first event', () => {
    const attachments = [{ artifactRef: `sha256:${'a'.repeat(64)}`, kind: 'image' as const,
      mediaType: 'image/png', filename: 'old.png', byteSize: 10 }]
    const restored = { ...initialRunState, timeline: [
      { id: 'prefix', kind: 'user' as const, text: 'prefix', turnIndex: 0, interactionId: 'old' },
      { id: 'restored', kind: 'user' as const, text: 'current', turnIndex: 1, interactionId: 'run-1', attachments },
      { id: 'partial', kind: 'assistant' as const, text: 'already streamed' },
    ] }
    const state = runReducer(restored, { type: 'event', event: event('run.started', { prompt: 'current' }) })
    expect(state.timeline.map((item) => item.text)).toEqual(['prefix', 'current'])
    expect(state.timeline[1].attachments).toEqual(attachments)
  })
  it('keeps turn identities distinct when each run restarts its event sequence', () => {
    let state = runReducer(initialRunState, { type: 'event', event: event('run.started', { prompt: 'first' }) })
    state = runReducer(state, { type: 'event', event: event('run.completed', {}, 2) })
    state = runReducer(state, {
      type: 'event', event: { ...event('run.started', { prompt: 'second' }), runId: 'run-2' },
    })
    expect(new Set(state.timeline.map((item) => item.id)).size).toBe(2)
  })
  it('associates Runtime duration with only its own turn and rejects invalid measurements', () => {
    let state = runReducer(initialRunState, { type: 'event', event: event('run.started', { prompt: 'first' }) })
    state = runReducer(state, { type: 'event', event: event('usage.updated', { elapsed_seconds: 155.9 }, 2) })
    state = runReducer(state, { type: 'event', event: event('run.completed', {}, 3) })
    state = runReducer(state, { type: 'event', event: event('run.started', { prompt: 'second' }, 4) })
    state = runReducer(state, { type: 'event', event: event('usage.updated', { elapsed_seconds: -1 }, 5) })
    expect(state.timeline.map((item) => item.elapsedSeconds)).toEqual([155.9, undefined])
    state = runReducer(state, { type: 'event', event: event('usage.updated', { elapsed_seconds: 8 }, 6) })
    expect(state.timeline.map((item) => item.elapsedSeconds)).toEqual([155.9, 8])
  })
  it('coalesces thinking deltas into one reasoning entry separate from the answer', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('run.started', { prompt: 'question' }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('thinking.delta', { text: 'considering ' }, 2),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('thinking.delta', { text: 'options' }, 3),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('assistant.delta', { text: 'answer' }, 4),
    })

    expect(state.timeline.map((item) => [item.kind, item.text])).toEqual([
      ['user', 'question'],
      ['thinking', 'considering options'],
      ['assistant', 'answer'],
    ])
  })

  it('projects streamed text and retractions without duplicating the final output', () => {    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('run.started', { prompt: 'inspect' }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('assistant.delta', { text: 'draft answer' }, 2),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('assistant.retracted', { characters: 6 }, 3),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('run.completed', { output: 'draft' }, 4),
    })

    expect(state.timeline.map((item) => [item.kind, item.text])).toEqual([
      ['user', 'inspect'],
      ['assistant', 'draft '],
    ])
    expect(state.status).toBe('completed')
  })

  it('retracts Unicode text across thinking segments', () => {
    let state = runReducer(initialRunState, {
      type: 'event', event: event('run.started', { prompt: 'question' }),
    })
    for (const item of [
      event('assistant.delta', { text: '保留🙂草' }, 2),
      event('thinking.delta', { text: 'thinking' }, 3),
      event('assistant.delta', { text: '稿🙂' }, 4),
      event('assistant.retracted', { characters: 3 }, 5),
    ]) state = runReducer(state, { type: 'event', event: item })
    expect(state.timeline.map((item) => [item.kind, item.text])).toEqual([
      ['user', 'question'], ['assistant', '保留🙂'], ['thinking', 'thinking'],
    ])
  })

  it('updates one tool card through approval and completion events', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('tool.started', {
        call_id: 'call-1',
        name: 'edit_file',
        args: { path: 'README.md' },
        risk: 'write',
        origin: 'builtin',
      }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('approval.pending', {
        call_id: 'call-1',
        name: 'edit_file',
        args: { path: 'README.md' },
        risk: 'write',
        origin: 'builtin',
        presentation: { title: 'edit_file · write · builtin', preview: 'diff', full_text: 'diff' },
      }, 2),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('approval.resolved', { call_id: 'call-1', approved: true, message: 'allowed' }, 3),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('tool.finished', {
        call_id: 'call-1',
        name: 'edit_file',
        result: 'updated',
        preview: 'updated',
        is_error: false,
      }, 4),
    })

    expect(state.timeline).toHaveLength(1)
    expect(state.timeline[0]).toMatchObject({
      kind: 'tool',
      callId: 'call-1',
      status: 'completed',
      pendingApproval: false,
      approved: true,
      result: 'updated',
    })
  })

  it('preserves schema-validated tool views for live and snapshot rendering', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('tool.started', {
        call_id: 'call-view',
        name: 'future_tool',
        args: { value: 1 },
        call_view: { schema_version: 1, title: 'Inspecting future value', detail: 'value 1' },
      }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('tool.finished', {
        call_id: 'call-view',
        name: 'future_tool',
        result: 'raw',
        is_error: false,
        result_view: { schema_version: 1, preview: 'done', full_text: 'full result' },
      }, 2),
    })

    expect(state.timeline[0].callView?.title).toBe('Inspecting future value')
    expect(state.timeline[0].resultView?.full_text).toBe('full result')
  })

  it('projects every request in a batch approval event', () => {
    const state = runReducer(initialRunState, {
      type: 'event',
      event: event('approval.batch_pending', {
        batch_id: 'batch-1',
        requests: [
          { call_id: 'call-1', name: 'edit_file', args: { path: 'a.py' } },
          { call_id: 'call-2', name: 'run_command', args: { argv: ['pytest'] } },
        ],
        presentations: [
          { title: 'Edit a.py', preview: 'diff a', full_text: 'full diff a' },
          { title: 'Run pytest', preview: 'pytest', full_text: 'pytest' },
        ],
      }),
    })

    expect(state.timeline).toHaveLength(2)
    expect(state.timeline.map((item) => item.callId)).toEqual(['call-1', 'call-2'])
    expect(state.timeline.every((item) => item.pendingApproval)).toBe(true)
    expect(state.timeline[1].presentation?.title).toBe('Run pytest')
  })

  it('updates the Agent tree from lifecycle events', () => {
    const state = runReducer(initialRunState, {
      type: 'event',
      event: event('agent.lifecycle', {
        agent_id: 'agent-1',
        path: '/root/explorer',
        phase: 'completed',
        status: 'completed',
        summary: 'Found the cause',
      }),
    })

    expect(state.agents[0]).toMatchObject({
      id: 'agent-1',
      path: '/root/explorer',
      status: 'completed',
      result_summary: 'Found the cause',
    })
    expect(state.timeline[0]).toMatchObject({ kind: 'agent', status: 'completed' })
  })

  it('hides control tools while preserving their plan and progress UI', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('run.started', { prompt: 'inspect' }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('tool.started', {
        call_id: 'control-1',
        name: 'set_plan',
        args: {},
        risk: 'read',
        origin: 'control',
      }, 2),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('plan.created', {
        plan: { revision: 1, steps: [{ id: 'one', title: 'Inspect', status: 'pending' }] },
      }, 3),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('progress.reported', { summary: '**Step 1:** inspect' }, 4),
    })

    expect(state.timeline.map((item) => item.kind)).toEqual(['user', 'plan', 'progress'])
    expect(state.timeline.some((item) => item.toolName === 'set_plan')).toBe(false)
  })

  it('projects work-product lifecycle events into the shared timeline', () => {
    const state = runReducer(initialRunState, {
      type: 'event',
      event: event('work_product.changed', {
        phase: 'verified',
        resource: 'report.md',
        status: 'verified',
        summary: 'target changed; non-target content unchanged',
      }),
    })

    expect(state.timeline[0]).toMatchObject({
      kind: 'work_product',
      status: 'verified',
      text: 'verified — report.md: target changed; non-target content unchanged',
    })
  })

  it('keeps the latest plan and terminal failure', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('plan.updated', {
        plan: { revision: 2, steps: [{ id: 'one', title: 'Inspect', status: 'in_progress' }] },
      }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('run.failed', { message: 'provider unavailable' }, 2),
    })

    expect(state.plan?.steps[0].title).toBe('Inspect')
    expect(state.timeline[0]).toMatchObject({
      kind: 'plan',
      plan: { revision: 2, steps: [{ id: 'one', title: 'Inspect', status: 'in_progress' }] },
    })
    expect(state.status).toBe('failed')
    expect(state.timeline.at(-1)).toMatchObject({ kind: 'error', text: 'provider unavailable', status: 'failed' })
  })

  it('updates a single live plan card after each step instead of waiting for completion', () => {
    let state = runReducer(initialRunState, { type: 'event', event: event('run.started', { prompt: 'work' }) })
    for (const [index, statuses] of [['pending', 'pending'], ['completed', 'in_progress'], ['completed', 'completed']].entries()) {
      state = runReducer(state, { type: 'event', event: event('plan.updated', {
        plan: { revision: 1, state_version: index + 1, steps: statuses.map((status, i) => ({ id: String(i), title: String(i), status })) },
      }, index + 2) })
      expect(state.status).toBe('running')
      const plans = state.timeline.filter(item => item.kind === 'plan')
      expect(plans).toHaveLength(1)
      expect(plans[0].plan?.steps.map(step => step.status)).toEqual(statuses)
    }
  })

  it('tracks plan review pending and resolved events', () => {
    const plan = {
      goal: 'Ship safely',
      revision: 3,
      state_version: 4,
      lifecycle: 'review_pending',
      approved_revision: null,
      steps: [{
        id: 'one',
        title: 'Inspect',
        depends_on: [],
        acceptance_criteria: [],
        status: 'pending' as const,
      }],
    }
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('plan.review_pending', { plan, revision: 3 }),
    })

    expect(state.planReviewStatus).toBe('review_pending')
    expect(state.planReviewRevision).toBe(3)
    expect(state.plan).toEqual(plan)

    state = runReducer(state, {
      type: 'event',
      event: event('plan.review_resolved', { revision: 3, approved: true }, 2),
    })
    expect(state.planReviewStatus).toBe('approved')
  })

  it('keeps plans inside their owning conversation turns', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('run.started', { prompt: 'first question' }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('plan.created', {
        plan: { revision: 1, steps: [{ id: 'one', title: 'First', status: 'pending' }] },
      }, 2),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('plan.updated', {
        plan: { revision: 2, steps: [{ id: 'one', title: 'First', status: 'completed' }] },
      }, 3),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('run.started', { prompt: 'second question' }, 4),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('plan.created', {
        plan: { revision: 1, steps: [{ id: 'two', title: 'Second', status: 'pending' }] },
      }, 5),
    })

    const plans = state.timeline.filter((item) => item.kind === 'plan')
    expect(plans).toHaveLength(2)
    expect(plans[0].plan?.steps[0]).toMatchObject({ title: 'First', status: 'completed' })
    expect(plans[1].plan?.steps[0]).toMatchObject({ title: 'Second', status: 'pending' })
  })

  it('round-trips blocking clarification into a waiting terminal state', () => {
    let state = runReducer(initialRunState, {
      type: 'event',
      event: event('clarification.requested', {
        question_id: 'clarify-1',
        question: 'Which target?',
        choices: ['A', 'B'],
      }),
    })
    state = runReducer(state, {
      type: 'event',
      event: event('run.waiting_for_user', {
        question_id: 'clarify-1',
        question: 'Which target?',
        choices: ['A', 'B'],
      }, 2),
    })

    expect(state.status).toBe('waiting_for_user')
    expect(state.runId).toBeNull()
    expect(state.timeline.at(-1)).toMatchObject({
      kind: 'system',
      status: 'waiting_for_user',
      text: 'Which target?\n- A\n- B',
    })
  })
})
