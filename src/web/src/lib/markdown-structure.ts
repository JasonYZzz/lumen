import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'

const parser = unified().use(remarkParse).use(remarkGfm)

interface MarkdownNode {
  type: string
  children?: MarkdownNode[]
  value?: string
  url?: string
  identifier?: string
  depth?: number
}

function nodeText(node: MarkdownNode): string {
  return node.value ?? node.children?.map(nodeText).join('') ?? ''
}

/** Parse rendered Markdown links/headings; code and image URLs are not references. */
export function markdownStructure(content: string) {
  const tree = parser.parse(content) as MarkdownNode
  const definitions = new Map<string, string>()
  const links: Array<{ url: string; title: string }> = []
  const headings: Array<{ title: string; depth: number; id: string }> = []
  function walk(node: MarkdownNode, visit: (node: MarkdownNode) => void) {
    visit(node)
    node.children?.forEach((child) => walk(child, visit))
  }
  walk(tree, (node) => {
    if (node.type === 'definition' && node.identifier && node.url) definitions.set(node.identifier, node.url)
  })
  walk(tree, (node) => {
    const url = node.type === 'link' ? node.url
      : node.type === 'linkReference' && node.identifier ? definitions.get(node.identifier) : undefined
    if (url) links.push({ url, title: nodeText(node) })
    if (node.type === 'heading') headings.push({ title: nodeText(node), depth: node.depth ?? 2, id: `section-${headings.length + 1}` })
  })
  return { links, headings }
}
