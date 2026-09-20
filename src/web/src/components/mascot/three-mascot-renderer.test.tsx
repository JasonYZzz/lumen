import { expect, it } from 'vitest'
import { mascotModelPath } from './three-mascot-renderer'
it('only accepts configured local mascot GLB paths', () => {
  expect(mascotModelPath(undefined)).toBeNull()
  expect(mascotModelPath('/mascot/fox.glb')).toBe('/mascot/fox.glb')
  for (const path of ['https://example.com/fox.glb', '//example.com/fox.glb', '/mascot/../private.glb', '/mascot/%2e%2e/x.glb', '/other/fox.glb', '/mascot/fox.gltf']) {
    expect(mascotModelPath(path)).toBeNull()
  }
})
