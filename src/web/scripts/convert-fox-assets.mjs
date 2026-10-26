import assert from 'node:assert/strict'
import { readdir, stat } from 'node:fs/promises'
import { createRequire } from 'node:module'
import { fileURLToPath } from 'node:url'

const require = createRequire(import.meta.url)
const sharp = require(require.resolve('sharp', { paths: [require.resolve('next')] }))
const directory = fileURLToPath(new URL('../public/new-design/', import.meta.url))
const sourceDirectory = fileURLToPath(new URL('../../../docs/brand/mascot/sources-v2/', import.meta.url))
const sources = (await readdir(sourceDirectory)).filter(name => /^fox-stage-0[1-5]-v2\.png$/.test(name)).sort()
assert.equal(sources.length, 5, 'Expected all five regenerated growth stages')
let inputBytes = 0
let outputBytes = 0
for (const [index, name] of sources.entries()) {
  assert.equal(name, `fox-stage-0${index + 1}-v2.png`)
  const source = `${sourceDirectory}/${name}`
  const original = await sharp(source).metadata()
  assert.ok(original.width >= 960 && original.height >= 960 && original.hasAlpha, `${name}: expected HD transparent source`)
  inputBytes += (await stat(source)).size
  for (const width of [original.width, 960, 480, 160]) {
    const suffix = width === original.width ? '' : `-${width}`
    const destination = `${directory}/fox-stage-0${index + 1}-v2${suffix}.webp`
    await sharp(source).rotate().resize({ width, withoutEnlargement: true })
      .webp({ lossless: true, effort: 6 }).toFile(destination)
    const converted = await sharp(destination).metadata()
    assert.equal(converted.format, 'webp')
    assert.equal(converted.width, Math.min(width, original.width))
    assert.equal(converted.hasAlpha, true)
    outputBytes += (await stat(destination)).size
  }
}
const blink = `${directory}/fox-stage-01-v2-blink.webp`
await sharp(`${sourceDirectory}/fox-stage-01-v2-blink.png`).resize(960).webp({ lossless: true, effort: 6 }).toFile(blink)
const blinkMetadata = await sharp(blink).metadata()
assert.equal(blinkMetadata.width, 960)
assert.equal(blinkMetadata.hasAlpha, true)
outputBytes += (await stat(blink)).size
console.log(`Converted 5 stages + blink, original resolution + 960px + 480px + 160px; PNG ${(inputBytes / 1048576).toFixed(2)} MiB → WebP ${(outputBytes / 1048576).toFixed(2)} MiB`)
