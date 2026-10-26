import { createHash } from 'node:crypto'
import { readFileSync } from 'node:fs'
import { expect, it } from 'vitest'
import source from './sources/ashen-press.html?raw'
import { librarySource, type LibraryBook } from './library-source'

const theme = {page:'#faf7f8',surface:'#ffffff',accent:'#795271',ink:'#302b33',line:'#e6dce4'}
const book: LibraryBook = {id:'one',title:'研究资料.md',type:'文档',revision:3,date:'2026/10/26',source:'来自对话'}

it('keeps the registered canonical source and shared styles byte-for-byte', () => {
  expect(createHash('sha256').update(source).digest('hex')).toBe('5fe2554e578acac5d55cb466a9564440e7767e38797981f8e829dfd2de0bc90f')
  const css = readFileSync(new URL('../threeui.css', import.meta.url))
  expect(createHash('sha256').update(css).digest('hex')).toBe('efe4447139f1358dd8e9be68edf6fa46cbefbd1de423a4d6c439ca61d2c8eccf')
})

it('adapts real metadata while retaining every authored shader and interaction', () => {
  const adapted = librarySource(source, [book], theme)
  for (const shader of source.matchAll(/(?:vertexShader|fragmentShader):\s*`[\s\S]*?`/g)) expect(adapted).toContain(shader[0])
  expect(adapted).toContain('three@0.181.0')
  expect(adapted).toContain('B.flipped?Math.PI:0')
  expect(adapted).toContain('DRAG.ry+=dx*0.0090')
  expect(adapted).toContain('function buildTearBand')
  expect(adapted).toContain('研究资料.md')
  expect(adapted).not.toContain("id:'nightreign'")
  expect(adapted).not.toContain('£68.00')
  expect(adapted).not.toContain('b.innerHTML=')
  const script = adapted.match(/<script type="module">([\s\S]*?)<\/script>/)![1]
  expect(() => new Function(script.replace("import * as THREE from 'three';", ''))).not.toThrow()
})

it('keeps hostile titles inside serialized data and DOM text nodes', () => {
  const adapted = librarySource(source, [{...book,title:'</script><img src=x onerror=alert(1)>\u2028'}], theme)
  expect(adapted).not.toContain('</script><img')
  expect(adapted).toContain('\\u003c/script>')
  expect(adapted).toContain('el.textContent=text')
})

it('rejects upstream drift instead of silently losing catalogue integration', () => {
  expect(() => librarySource(source.replace('const BOOKS = [', 'const VOLUMES = ['), [book], theme)).toThrow('registered integration')
})
