'use client'

import { useCallback, useEffect, useState } from 'react'
import { lumenApi } from '@/lib/api/client'
import './assistant-workspace.css'

type Preferences = Awaited<ReturnType<typeof lumenApi.preferences>>

type VikingOperation = 'status' | 'enable' | 'disable' | 'backfill' | 'purge'

function SemanticMemory({ ensureSession }: { ensureSession: () => Promise<string> }) {
  const [scope, setScope] = useState<'project' | 'user'>('project')
  const [result, setResult] = useState<Awaited<ReturnType<typeof lumenApi.memoryControl>> | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [confirmPurge, setConfirmPurge] = useState(false)
  const apply = async (operation: VikingOperation) => {
    setBusy(true)
    setError('')
    try {
      setResult(await lumenApi.memoryControl(await ensureSession(), 'openviking', { operation, scope }))
      setConfirmPurge(false)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '操作失败，请重新读取状态')
    } finally {
      setBusy(false)
    }
  }
  return <fieldset disabled={busy} className="assistant-form">
    <legend>OpenViking 语义记忆</legend>
    <label>同步范围
      <select value={scope} onChange={event => {
        setScope(event.target.value === 'user' ? 'user' : 'project')
        setConfirmPurge(false)
        setResult(null)
        setError('')
      }}>
        <option value="project">当前项目</option>
        <option value="user">跨项目个人记忆</option>
      </select>
    </label>
    <button type="button" onClick={() => void apply('status')}>读取同步状态</button>
    {error && <p role="alert">{error}</p>}
    {result && <p role="status">{result.message}</p>}
    {result?.payload.configured === true && <>
      {result.payload.enabled === true ? <>
        <button type="button" onClick={() => void apply('disable')}>停用此范围</button>
        <button type="button" onClick={() => void apply('backfill')}>同步现有已确认条目</button>
      </> : <button type="button" onClick={() => void apply('enable')}>授权此范围</button>}
      {confirmPurge ? <>
        <p>将停用选定范围，并移除该服务上的记忆副本。</p>
        <button type="button" onClick={() => void apply('purge')}>确认移除</button>
        <button type="button" onClick={() => setConfirmPurge(false)}>取消</button>
      </> : <button type="button" onClick={() => setConfirmPurge(true)}>移除远端副本</button>}
    </>}
  </fieldset>
}

export function PersonalPreferences({ ensureSession }: { ensureSession?: () => Promise<string> } = {}) {
  const [data, setData] = useState<Preferences | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [content, setContent] = useState('')
  const [scope, setScope] = useState<'user' | 'project'>('project')
  const [editing, setEditing] = useState<Preferences['records'][number] | null>(
    null,
  )
  const [forgetting, setForgetting] = useState<string | null>(null)
  const load = useCallback(async () => {
    setLoading(true)
    try {
      setData(await lumenApi.preferences())
      setError('')
      return true
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '无法读取偏好')
      return false
    } finally {
      setLoading(false)
    }
  }, [])
  useEffect(() => {
    void load()
  }, [load])
  const apply = async (body: Parameters<typeof lumenApi.editPreference>[0]) => {
    setBusy(true)
    setError('')
    try {
      await lumenApi.editPreference(body)
      if (!(await load())) return
      setEditing(null)
      setContent('')
      setForgetting(null)
    } catch (reason) {
      setError(
        reason instanceof Error ? reason.message : '保存失败，请重新读取',
      )
    } finally {
      setBusy(false)
    }
  }
  return (
    <div className="assistant-preferences">
      <div className="settings-section-heading">
        <div>
          <h2>个性化偏好</h2>
          <p>查看、确认或遗忘 Lumen 用于理解你的偏好。偏好不会替代执行授权。</p>
        </div>
      </div>
      <p>用户范围会跨项目共享；项目与路径范围保留现有隔离规则。</p>
      {ensureSession && <SemanticMemory ensureSession={ensureSession} />}
      {error && (
        <div role="alert" className="settings-inline-error">
          {error}
          <button
            type="button"
            disabled={loading || busy}
            onClick={() => void load()}
          >
            重新读取
          </button>
        </div>
      )}
      {!data ? (
        loading ? (
          <p role="status">正在读取…</p>
        ) : (
          <p>尚未读取到偏好，请重新读取。</p>
        )
      ) : !data.available ? (
        <p>当前 Host 未配置偏好存储。</p>
      ) : (
        <>
          <form
            className="assistant-form"
            onSubmit={(event) => {
              event.preventDefault()
              void apply(
                editing
                  ? {
                      action: 'edit',
                      id: editing.id,
                      content,
                      expected_updated_at: editing.updated_at,
                    }
                  : { action: 'remember', content, scope },
              )
            }}
          >
            <label>
              {editing ? '编辑偏好' : '明确告诉 Lumen 一项偏好'}
              <textarea
                required
                disabled={busy}
                maxLength={4000}
                value={content}
                onChange={(event) => setContent(event.target.value)}
                placeholder="例如：工作简报使用中文，先列出需要我决定的事项。"
              />
            </label>
            {!editing && (
              <label>
                生效范围
                <select
                  disabled={busy}
                  value={scope}
                  onChange={(event) =>
                    setScope(event.target.value as typeof scope)
                  }
                >
                  <option value="project">当前项目</option>
                  <option value="user">用户（跨项目共享）</option>
                </select>
              </label>
            )}
            <div className="assistant-card-actions">
              <button className="assistant-primary" disabled={busy}>
                保存偏好
              </button>
              {editing && (
                <button
                  type="button"
                  disabled={busy}
                  onClick={() => {
                    setEditing(null)
                    setContent('')
                  }}
                >
                  取消编辑
                </button>
              )}
            </div>
          </form>
          {data.records.map((record) => (
            <article key={record.id} className="assistant-preference-row">
              <p>{record.content}</p>
              <small>
                {record.scope} ·{' '}
                {record.source_kind === 'explicit'
                  ? '用户明确提供'
                  : record.source_kind === 'observed'
                    ? '观察所得'
                    : '推测'}{' '}
                ·{' '}
                {record.status === 'candidate' ? '候选，待确认' : record.status}
              </small>
              <div className="assistant-card-actions">
                <button
                  disabled={busy}
                  onClick={() => {
                    setEditing(record)
                    setContent(record.content)
                  }}
                >
                  编辑
                </button>
                {record.source_kind !== 'explicit' && (
                  <button
                    disabled={busy}
                    onClick={() =>
                      void apply({
                        action: 'confirm',
                        id: record.id,
                        expected_updated_at: record.updated_at,
                      })
                    }
                  >
                    确认这项偏好
                  </button>
                )}
                <button
                  disabled={busy}
                  onClick={() => setForgetting(record.id)}
                >
                  遗忘
                </button>
              </div>
              {forgetting === record.id && (
                <div role="group" aria-label="确认遗忘">
                  <p>遗忘后将停止召回这项偏好；历史会话仍保留。</p>
                  <button
                    disabled={busy}
                    onClick={() =>
                      void apply({
                        action: 'forget',
                        id: record.id,
                        expected_updated_at: record.updated_at,
                      })
                    }
                  >
                    确认遗忘
                  </button>
                  <button disabled={busy} onClick={() => setForgetting(null)}>
                    保留
                  </button>
                </div>
              )}
            </article>
          ))}
          {data.records.length === 0 && (
            <p>还没有保存的偏好。你可以先明确写下一项。</p>
          )}
        </>
      )}
    </div>
  )
}
