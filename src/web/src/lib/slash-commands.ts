import type { Bootstrap } from './api/types'

export interface SlashCommand {
  value: string
  description: string
  label?: string
  keywords?: string
  kind?: 'command' | 'skill' | 'model' | 'mode'
}

export const baseSlashCommands: SlashCommand[] = [
  { value: '/model', label: '模型', description: '选择下一轮使用的模型', keywords: '模型 model' },
  { value: '/thinking', label: '推理强度', description: '选择此任务下一轮的推理档位', keywords: 'reasoning effort' },
  { value: '/tasks', label: '计划记录', description: '在运行记录中查看计划历史', keywords: 'plan 计划 步骤' },
  { value: '/mode', label: '审批模式', description: '选择工具操作的确认方式', keywords: 'approval' },
  { value: '/copy', label: '复制回答', description: '将最新回复复制到剪贴板' },
  { value: '/new', label: '新建任务', description: '开始一段新对话', keywords: 'session task' },
  { value: '/retry', description: '重试上次任务', keywords: 'again run' },
  { value: '/agents', description: '查看和协调子 Agent', keywords: 'subagent worker' },
  { value: '/checkpoints', label: '分支与历史', description: '查看检查点，从历史创建分支', keywords: 'rewind fork' },
  { value: '/transcript', description: '搜索结构化 transcript', keywords: 'audit history' },
  { value: '/dequeue', description: '撤回尚未执行的排队输入', keywords: 'queue restore' },
  { value: '/clear', description: '清空当前显示', keywords: 'timeline' },
  {
    value: '/plan ',
    description: '先生成计划，确认后再执行任务',
    keywords: 'planning collaboration',
    kind: 'mode',
  },
  { value: '/context', description: '查看上下文', keywords: 'tokens' },
  { value: '/context sources', description: '查看当前会话上下文来源' },
  { value: '/instructions', description: '查看稳定 prompt、动态上下文与来源' },
  { value: '/compact ', description: '压缩上下文', keywords: 'focus' },
  { value: '/memory ', description: '管理记忆', keywords: 'remember forget list' },
  { value: '/tools', label: '工具', description: '查看当前可用能力' },
  { value: '/skills', label: '技能', description: '查看可用的领域流程' },
  { value: '/skill unload ', description: '卸载当前会话 Skill' },
  { value: '/resource ', description: '激活 MCP resource' },
  { value: '/resource refresh ', description: '刷新当前会话 MCP resource 快照' },
  { value: '/resource unload ', description: '卸载 MCP resource' },
  { value: '/clarification cancel', description: '取消待回答澄清' },
  { value: '/mcp', description: '查看 MCP 连接' },
  { value: '/prompts', description: '查看 MCP prompt 模板' },
  { value: '/prompt ', description: '渲染并运行 MCP prompt 模板' },
  { value: '/hooks', description: '查看 hooks 与触发统计' },
  { value: '/help', description: '查看命令帮助' },
]

/** Display labels, help text, and inserted commands have separate roles. */
export function buildSlashCommands(
  bootstrap?: Pick<Bootstrap, 'availableModels' | 'activeModel' | 'skills'> | null,
): SlashCommand[] {
  return [
    ...baseSlashCommands,
    ...(bootstrap?.availableModels ?? []).map((model): SlashCommand => ({
      value: `/model ${model}`,
      label: model,
      description: model === bootstrap?.activeModel ? '当前模型' : '切换模型',
      keywords: 'model',
      kind: 'model',
    })),
    { value: '/mode manual', label: '每次确认', description: '操作前请求确认', keywords: 'approval', kind: 'mode' },
    { value: '/mode accept_edits', label: '自动接受文件修改', description: '其他操作仍按策略审批', keywords: 'approval', kind: 'mode' },
    { value: '/mode auto', label: '自动执行', description: '按自动审批策略执行', keywords: 'approval', kind: 'mode' },
    ...(bootstrap?.skills ?? []).map((skill): SlashCommand => ({
      value: `/skill:${skill.name} `,
      label: skill.name,
      description: skill.description || '运行技能',
      keywords: 'skill',
      kind: 'skill',
    })),
  ]
}

export interface PromptInvocation {
  reference: string
  arguments: Record<string, string>
}

function splitCommandLine(value: string): string[] {
  const words: string[] = []
  let word = ''
  let quote: 'single' | 'double' | null = null
  let escaped = false
  let started = false

  for (const character of value.trim()) {
    if (escaped) {
      word += character
      escaped = false
      started = true
      continue
    }
    if (character === '\\' && quote !== 'single') {
      escaped = true
      started = true
      continue
    }
    if (character === "'" && quote !== 'double') {
      quote = quote === 'single' ? null : 'single'
      started = true
      continue
    }
    if (character === '"' && quote !== 'single') {
      quote = quote === 'double' ? null : 'double'
      started = true
      continue
    }
    if (/\s/.test(character) && quote === null) {
      if (started) words.push(word)
      word = ''
      started = false
      continue
    }
    word += character
    started = true
  }
  if (quote !== null) throw new Error('prompt 参数的引号未闭合')
  if (escaped) word += '\\'
  if (started) words.push(word)
  return words
}

export function parsePromptInvocation(value: string): PromptInvocation {
  const words = splitCommandLine(value)
  if (words[0]?.toLowerCase() !== '/prompt' || !words[1]) {
    throw new Error('用法：/prompt <server:name> [key=value ...]')
  }
  const arguments_: Record<string, string> = {}
  for (const item of words.slice(2)) {
    const separator = item.indexOf('=')
    if (separator <= 0) throw new Error(`无效 prompt 参数：${item}；请使用 key=value`)
    arguments_[item.slice(0, separator)] = item.slice(separator + 1)
  }
  return { reference: words[1], arguments: arguments_ }
}

export function slashQuery(value: string): string | null {
  return /^\/\S*$/.test(value) ? value.slice(1).toLowerCase() : null
}

export function filterSlashCommands(commands: SlashCommand[], value: string): SlashCommand[] {
  const argumentQuery = value.match(/^\/(model|mode)\s+(.*)$/i)
  if (argumentQuery) {
    const prefix = `/${argumentQuery[1].toLowerCase()} `
    const query = argumentQuery[2].trim().toLowerCase()
    return commands.filter((item) => item.value.toLowerCase().startsWith(prefix)
      && item.value.slice(prefix.length).toLowerCase().includes(query))
      .sort((a, b) => Number(b.value.slice(prefix.length).toLowerCase() === query)
        - Number(a.value.slice(prefix.length).toLowerCase() === query))
  }
  const query = slashQuery(value)
  if (query == null) return []
  if (!query) return commands.filter((item) => !/^\/(model|mode)\s/.test(item.value))
  return commands.filter((item) => {
    const haystack = `${item.value.slice(1)} ${item.label ?? ''} ${item.description} ${item.keywords ?? ''}`.toLowerCase()
    return haystack.includes(query)
  })
}
