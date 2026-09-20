import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { expect, it } from 'vitest'
it('flushes buffered PCM immediately on interruption and accepts subsequent audio', () => {
  const processors = new Map<string, new (options: object) => {
    port: { onmessage: (event: { data: unknown }) => void }
    process: (inputs: unknown[], outputs: Float32Array[][]) => boolean
  }>()
  runInNewContext(readFileSync(new URL('../../../public/lumen-pcm-worklet.js', import.meta.url), 'utf8'), {
    sampleRate: 48000, ArrayBuffer, Int16Array, Float32Array,
    AudioWorkletProcessor: class { port = {} },
    registerProcessor: (name: string, processor: never) => processors.set(name, processor),
  })
  const Playback = processors.get('lumen-pcm-playback')!, playback = new Playback({ processorOptions: { sourceRate: 24000 } })
  playback.port.onmessage({ data: new Int16Array([16384, 16384, 16384, 16384]).buffer })
  const output = new Float32Array(2); playback.process([], [[output]])
  expect([...output]).toEqual([.5, .5])
  playback.port.onmessage({ data: { type: 'clear' } }); playback.process([], [[output]])
  expect([...output]).toEqual([0, 0])
  playback.port.onmessage({ data: new Int16Array([8192, 8192, 8192, 8192]).buffer }); playback.process([], [[output]])
  expect([...output]).toEqual([.25, .25])
})
