import { readFile, readdir, writeFile } from 'node:fs/promises'
import { resolve, relative } from 'node:path'
import { gzipSync, brotliCompressSync } from 'node:zlib'

const web = resolve(import.meta.dirname, '..'), out = resolve(web, 'out')
async function sizes(path) {
  const source = await readFile(path)
  return { path: '/' + relative(out, path), bytes: source.length, gzipBytes: gzipSync(source).length, brotliBytes: brotliCompressSync(source).length }
}
async function scripts(path) {
  const html = await readFile(resolve(out, path), 'utf8')
  const paths = [...new Set([...html.matchAll(/<script[^>]+src="([^"]+)"/g)].map(match => match[1].split('?')[0]))]
  const files = await Promise.all(paths.filter(path => path.startsWith('/_next/')).map(path => sizes(resolve(out, '.' + path))))
  return { files, gzipBytes: files.reduce((sum, file) => sum + file.gzipBytes, 0) }
}
const chunks = []
async function walk(path) {
  for (const entry of await readdir(path, { withFileTypes: true })) {
    const file = resolve(path, entry.name)
    if (entry.isDirectory()) await walk(file)
    else if (entry.name.endsWith('.js')) chunks.push(await sizes(file))
  }
}
await walk(resolve(out, '_next/static/chunks'))
const report = {
  date: new Date().toISOString(), node: process.version,
  kind: 'Static exported script/chunk bytes; not measured requests, latency, frames or GPU cost. Dynamic chunks may be shared between scenes.',
  routes: { '/': await scripts('index.html'), '/scene-preview.html': await scripts('scene-preview.html') },
  chunks: chunks.sort((a, b) => b.gzipBytes - a.gzipBytes),
}
if (process.argv[2]) await writeFile(resolve(process.argv[2]), JSON.stringify(report, null, 2) + '\n')
console.log(JSON.stringify({ rootGzipBytes: report.routes['/'].gzipBytes, rootScripts: report.routes['/'].files.length, largestChunks: report.chunks.slice(0, 5) }, null, 2))
