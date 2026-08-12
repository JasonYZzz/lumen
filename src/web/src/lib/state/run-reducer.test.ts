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
  it('projects streamed text and retractions without duplicating the final output', () => {
    let state = runReducer(initialRunState, {
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
    expect(state.timeline.at(-1)).toMatchObject({ kind: 'error', text: 'provider unavailable' })
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
