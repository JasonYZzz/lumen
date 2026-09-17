'use client'

import { useMemo, useState } from 'react'
import type { TimelineEntry } from '../lib/api/types'
import { toolOutput } from '../lib/tool-output'
import { CopyButton } from './copy-button'
import { MarkdownMessage } from './markdown-message'
import { WebLink } from './web-link'

const DISPLAY_LIMIT = 16_000

function OutputText({ text, label }: { text: string; label: string }) {
  return <section className="tool-output-text"><header><strong>{label}</strong><CopyButton text={text} label={`复制${label}`} className="code-copy-button" /></header>
    <pre>{text.slice(0, DISPLAY_LIMIT) || '（无内容）'}</pre>
    {text.length > DISPLAY_LIMIT && <p className="output-limit" role="note">当前显示前 {DISPLAY_LIMIT.toLocaleString()} 个字符；复制可取得此条记录中保存的文本。</p>}
  </section>
}

export function ToolOutputDetails({ item }: { item: TimelineEntry }) {
  const [raw, setRaw] = useState(false)
  const output = useMemo(() => toolOutput(item), [item])
  const result = item.result ?? (typeof item.resultView?.full_text === 'string' ? item.resultView.full_text : '')
  const truncated = item.resultView?.truncated === true
    || output?.kind === 'command' && output.truncated
    || (output?.kind === 'page' || output?.kind === 'file') && output.hasMore
  return <div className="tool-detail">
    <div className="tool-output-toolbar">
      <strong>{output && !raw ? '结果' : '原始记录'}</strong>
      {output && <button type="button" onClick={() => setRaw(!raw)} aria-pressed={raw}>{raw ? '查看结果' : '查看原始记录'}</button>}
    </div>
    {truncated && <p className="output-limit" role="note">输出包含截断或分页内容；这里只展示当前记录可用的片段。</p>}
    {output && !raw ? <div className="tool-readable-output">
      {output.kind === 'search' && <>
        <p className="tool-result-meta">搜索：{output.query} · {output.sources.length} 条结果</p>
        {output.warnings.length > 0 && <p className="output-limit">部分搜索引擎未响应：{output.warnings.join('；')}</p>}
        {output.sources.length ? <ol className="search-results">{output.sources.map((source, index) => <li key={`${source.url}-${index}`}>
          <strong><WebLink href={source.url}>{source.title}</WebLink></strong>
          <small>{new URL(source.url).hostname}{source.published ? ` · ${source.published}` : ''}</small>
          {source.snippet && <p>{source.snippet}</p>}
        </li>)}</ol> : <p>未找到可展示的网页结果。</p>}
      </>}
      {output.kind === 'page' && <>
        <strong><WebLink href={output.source.url}>{output.source.title}</WebLink></strong>
        <p className="tool-result-meta">{new URL(output.source.url).hostname}{output.source.published ? ` · ${output.source.published}` : ''}</p>
        <MarkdownMessage content={output.content.slice(0, DISPLAY_LIMIT)} />
        {output.content.length > DISPLAY_LIMIT && <p className="output-limit">阅读视图显示前 {DISPLAY_LIMIT.toLocaleString()} 个字符，更多内容可在原始记录中查看。</p>}
      </>}
      {output.kind === 'file' && <><p className="tool-result-meta">{output.path}{output.range ? ` · ${output.range}` : ''}</p>
        <OutputText text={output.content} label="文件内容" /></>}
      {output.kind === 'command' && <>
        <p className="tool-result-meta">退出码 {output.exitCode}</p><pre className="tool-command">{output.command}</pre>
        <OutputText text={output.stdout} label="标准输出" />
        {output.stderr && <OutputText text={output.stderr} label="标准错误" />}
      </>}
    </div> : <>
      {Object.keys(item.args ?? {}).length > 0 && <OutputText text={JSON.stringify(item.args, null, 2)} label="输入" />}
      <OutputText text={result} label="输出" />
    </>}
  </div>
}
