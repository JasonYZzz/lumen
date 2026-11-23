import { CaretRight, Plus, X } from '@phosphor-icons/react'
import { useId, useRef, useState } from 'react'
import { SettingsList } from './settings-list'
import { lumenApi, type SkillPreview } from '@/lib/api/client'
import type { CapabilityInventory } from '@/lib/api/types'

type Source = 'zip' | 'directory' | 'github'
type Scope = 'project' | 'user'

const statusLabels: Record<string, string> = { loaded: '已启用', manual: '仅手动', disabled: '已停用', shadowed: '已覆盖' }
const sourceLabels: Record<string, string> = {
  user: '用户', project: '项目', builtin: '内置', 'shared-user': '用户共享', 'shared-project': '项目共享',
}

async function folderBody(files: File[]) {
  const entries: Record<string, { content: string; executable: boolean }> = Object.create(null)
  for (const file of files) {
    const name = file.webkitRelativePath
    if (!name || Object.hasOwn(entries, name)) throw new Error('目录文件路径无效')
    const content = await new Promise<string>((resolve, reject) => {
      const reader = new FileReader()
      reader.onload = () => resolve(String(reader.result).split(',')[1] ?? '')
      reader.onerror = () => reject(new Error('读取文件失败'))
      reader.readAsDataURL(file)
    })
    entries[name] = { content, executable: false }
  }
  return JSON.stringify({ files: entries })
}

export function SkillSettings({ inventory, configRevision, ensureSession, busy, onChange }: {
  inventory: CapabilityInventory | null
  configRevision?: string
  ensureSession: () => Promise<string>
  busy: boolean
  onChange: () => Promise<void>
}) {
  const [importOpen, setImportOpen] = useState(false)
  const panelId = useId()
  const importToggle = useRef<HTMLButtonElement>(null)
  const importSource = useRef<HTMLSelectElement>(null)
  const [kind, setKind] = useState<Source>('zip')
  const [scope, setScope] = useState<Scope>('project')
  const [github, setGithub] = useState('')
  const [files, setFiles] = useState<File[]>([])
  const [preview, setPreview] = useState<SkillPreview | null>(null)
  const [session, setSession] = useState('')
  const [path, setPath] = useState('')
  const [message, setMessage] = useState('')
  const [pending, setPending] = useState(false)
  const [remove, setRemove] = useState('')
  const locked = useRef(false)
  const candidate = preview?.candidates.find(item => item.path === path)
  const available = inventory?.tools.some(tool => tool.name === 'install_skill' &&
    tool.status !== 'disabled' && tool.approval !== 'deny')

  const act = async (operation: () => Promise<void>) => {
    if (locked.current) return
    locked.current = true
    setPending(true)
    setMessage('')
    try { await operation() }
    catch (error) { setMessage(error instanceof Error ? error.message : '操作失败，请重试。') }
    finally { locked.current = false; setPending(false) }
  }
  const inspect = async () => {
    setPreview(null)
    if (files.length > 4096 || files.reduce((sum, file) => sum + file.size, 0) > 32 * 1024 * 1024) {
      throw new Error('技能包最多 4096 个文件、32 MiB。')
    }
    const body = kind === 'github' ? github.trim() : kind === 'zip' ? files[0] : await folderBody(files)
    const id = await ensureSession()
    const result = await lumenApi.previewSkill(id, kind, scope, body)
    setSession(id)
    setPreview(result)
    setPath(result.candidates.length === 1 ? result.candidates[0].path : '')
  }
  const form = available ? <fieldset className="skill-import" disabled={busy || pending}
    hidden={!importOpen} id={panelId} aria-label="导入技能">
      <div className="skill-import-options">
        <label>导入来源 <select ref={importSource} value={kind} onChange={e => {
          setKind(e.target.value as Source); setFiles([]); setPreview(null)
        }}><option value="zip">ZIP 文件</option><option value="directory">文件夹</option><option value="github">GitHub</option></select></label>
        <label>安装范围 <select value={scope} onChange={e => { setScope(e.target.value as Scope); setPreview(null) }}>
          <option value="project">当前项目</option><option value="user">当前用户</option></select></label>
      </div>
      <div className="skill-import-input">
        {kind === 'github' ? <input aria-label="GitHub 仓库或 Skill URL" value={github} onChange={e => {
          setGithub(e.target.value); setPreview(null)
        }} placeholder="owner/repo 或 GitHub URL" /> : <input key={kind} type="file" aria-label="选择技能包"
          accept={kind === 'zip' ? '.zip' : undefined}
          {...(kind === 'directory' ? { webkitdirectory: '', multiple: true } : {})}
          onChange={e => { setFiles(Array.from(e.target.files ?? [])); setPreview(null) }} />}
        {kind === 'directory' && <small>需要保留空目录和执行位时请选择 ZIP。</small>}
        <button disabled={kind === 'github' ? !github.trim() : !files.length} onClick={() => void act(inspect)}>预览技能包</button>
      </div>
        {preview && <div className="skill-preview"><p>{preview.source} · {preview.revision} · {preview.files} 个文件 · {preview.bytes} 字节</p>
          <label>候选技能 <select value={path} onChange={e => setPath(e.target.value)}>
            <option value="">选择要安装的技能</option>
            {preview.candidates.map(c => <option key={c.path} value={c.path}>{c.name} · {c.path}</option>)}
          </select></label>
          {candidate && <><p>{candidate.description}</p><p>{candidate.manual_only ? '仅手动调用' : '允许自动调用'}
            {candidate.scripts.length > 0 && ` · 声明脚本：${candidate.scripts.join('、')}`}</p>
            {candidate.diagnostics.map(d => <p key={d}>{d}</p>)}
            <details><summary>查看指令节选</summary><pre>{candidate.body}</pre></details>
            {candidate.installed_revision !== 'missing' &&
              <p>已有来源 {candidate.installed_source || '本地目录'} · {candidate.installed_revision}</p>}
            {candidate.locally_modified && <p>目标包含本地修改或未受管文件，请先保留原目录。</p>}
            <button disabled={candidate.locally_modified} onClick={() => void act(async () => {
              if (await ensureSession() !== session) throw new Error('会话已切换，请重新预览技能包。')
              const result = await lumenApi.confirmSkill(session, preview, path, candidate.installed_revision !== 'missing')
              if (!['installed', 'updated', 'already_installed'].includes(result.status)) throw new Error('目标已变化，请重新预览。')
              setPreview(null); await onChange(); setImportOpen(false)
              requestAnimationFrame(() => importToggle.current?.focus())
              setMessage(candidate.user_invocable ? `可以在会话中输入 /skill:${result.name}` : '安装完成。作者已关闭手动调用。')
            })}>{candidate.installed_revision === 'missing' ? '确认安装' : '确认更新此安装'}</button></>}
        </div>}
  </fieldset> : <p className="extension-empty">在 tools.builtins 中启用 install_skill 后可导入技能。</p>

  return <SettingsList title="Skills" items={inventory?.skills ?? []} empty="尚未发现技能。"
    action={available && <button type="button" className="extension-primary" ref={importToggle}
      aria-controls={panelId} aria-expanded={importOpen} disabled={busy || pending} onClick={() => setImportOpen(!importOpen)}>
      {importOpen ? <X size={14} /> : <Plus size={14} />}{importOpen ? '关闭导入' : '导入技能'}
    </button>} beforeList={<>{form}{message && <p className="skill-feedback" role="status">{message}</p>}</>}>
    {item => {
      const location = String(item.path ?? '')
      const name = String(item.name)
      const status = String(item.status)
      const source = String(item.source ?? '')
      const managed = item.managed as { source?: string; ref?: string; path?: string; content_revision?: string } | undefined
      return <details className="skill-entry" key={location || name} onToggle={e => {
        if (!e.currentTarget.open && remove === location) setRemove('')
      }}>
        <summary><span className="skill-entry-title"><strong>{name}</strong>
          <small>{sourceLabels[source] ?? source}{item.description ? ` · ${String(item.description)}` : ''}</small>
        </span><span className="skill-state" data-status={status}>{statusLabels[status] ?? status}</span>
          <CaretRight className="skill-caret" size={14} aria-hidden="true" /></summary>
        <div className="skill-detail">
          {!!item.description && <p>{String(item.description)}</p>}
          <dl>
            {item.user_invocable !== false && ['manual', 'loaded'].includes(status) &&
              <><dt>调用命令</dt><dd><code>/skill:{name}</code></dd></>}
            {location && <><dt>文件位置</dt><dd>{location}</dd></>}
            {managed?.source && <><dt>安装来源</dt><dd>{managed.source}</dd></>}
            {managed?.ref && <><dt>版本</dt><dd><code>{managed.ref}</code></dd></>}
          </dl>
          {Array.isArray(item.diagnostics) && item.diagnostics.map(note => <p key={String(note)}>{String(note)}</p>)}
          {location && available && <fieldset className="skill-actions" disabled={busy || pending} aria-label={`管理 ${name}`}>
          {managed?.source?.startsWith('https://github.com/') && <button type="button" onClick={() => {
            setKind('github'); setGithub(managed.path && managed.path !== '.'
              ? `${managed.source}/tree/HEAD/${managed.path.split('/').map(encodeURIComponent).join('/')}`
              : managed.source!); setFiles([]); setPreview(null)
            setScope(item.source === 'user' ? 'user' : 'project')
            setImportOpen(true)
            requestAnimationFrame(() => importSource.current?.focus())
            setMessage(`请预览更新并核对候选目录 ${managed.path ?? '.'}。`)
          }}>更新</button>}
          <button type="button" disabled={!configRevision} onClick={() => void act(async () => {
            await lumenApi.manageSkill(await ensureSession(), location, item.status === 'disabled' ? 'enable' : 'disable', configRevision!)
            await onChange()
          })}>{item.status === 'disabled' ? '启用' : '停用'}</button>
          {managed?.content_revision && <button type="button" className="is-danger" onClick={() => {
            if (remove !== location) { setRemove(location); return }
            void act(async () => {
              await lumenApi.manageSkill(await ensureSession(), location, 'remove', managed.content_revision!)
              setRemove(''); await onChange()
            })
          }}>{remove === location ? '确认移除受管安装' : '移除'}</button>}
          {remove === location && <button onClick={() => setRemove('')}>取消</button>}
          </fieldset>}
        </div>
      </details>
    }}
  </SettingsList>
}
