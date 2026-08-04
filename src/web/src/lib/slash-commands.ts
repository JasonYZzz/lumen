export interface SlashCommand {
  value: string
  description: string
  keywords?: string
}

export const baseSlashCommands: SlashCommand[] = [
  { value: '/new', description: '新建任务', keywords: 'session task' },
  { value: '/retry', description: '重试上次任务', keywords: 'again run' },
  { value: '/clear', description: '清空当前显示', keywords: 'timeline' },
  { value: '/model ', description: '切换模型' },
  { value: '/mode ', description: '切换审批模式', keywords: 'approval' },
  { value: '/context', description: '查看上下文', keywords: 'tokens' },
  { value: '/context sources', description: '查看当前会话上下文来源' },
  { value: '/compact ', description: '压缩上下文', keywords: 'focus' },
  { value: '/memory ', description: '管理记忆', keywords: 'remember forget list' },
  { value: '/tools', description: '查看可用工具' },
  { value: '/skills', description: '查看可用技能' },
  { value: '/skill unload ', description: '卸载当前会话 Skill' },
  { value: '/resource ', description: '激活 MCP resource' },
  { value: '/resource refresh ', description: '刷新当前会话 MCP resource 快照' },
  { value: '/resource unload ', description: '卸载 MCP resource' },
  { value: '/clarification cancel', description: '取消待回答澄清' },
  { value: '/mcp', description: '查看 MCP 连接' },
  { value: '/prompts', description: '查看 MCP prompt 模板' },
  { value: '/prompt ', description: '渲染并运行 MCP prompt 模板' },
  { value: '/hooks', description: '查看 hooks 与触发统计' },
  { value: '/copy', description: '复制最新回复' },
  { value: '/help', description: '查看命令帮助' },
]

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
  const query = slashQuery(value)
  if (query == null) return []
  if (!query) return commands
  return commands.filter((item) => {
    const haystack = `${item.value.slice(1)} ${item.description} ${item.keywords ?? ''}`.toLowerCase()
    return haystack.includes(query)
  })
}
