import { isValidElement, type ReactNode } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { CopyButton } from './copy-button'
import { DocumentCode, DocumentLink } from './document-preview'

function CodeBlock({ children }: { children?: ReactNode }) {
  const code = isValidElement<{ children?: string; className?: string }>(children) ? children : null
  const text = String(code?.props.children ?? '')
  const language = code?.props.className?.replace(/^language-/, '') ?? 'text'
  return <div className="markdown-code-block">
    <div className="code-block-toolbar"><span>{language}</span><CopyButton text={text} label="复制代码" className="code-copy-button" showLabel /></div>
    <pre><code className={code?.props.className}>{text}</code></pre>
  </div>
}

export function MarkdownMessage({ content }: { content: string }) {
  return (
    <div className="markdown-body">
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={{
        pre: CodeBlock,
        a: ({ href, children }) => <DocumentLink href={href ?? ''}>{children}</DocumentLink>,
        code: DocumentCode,
        table: ({ children }) => <div className="markdown-table-scroll" role="region" aria-label="表格（可横向滚动）" tabIndex={0}><table>{children}</table></div>,
      }}>{content}</ReactMarkdown>
    </div>
  )
}
