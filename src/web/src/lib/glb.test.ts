import { describe, expect, it, vi } from 'vitest'
import { Mesh, MeshStandardMaterial, BufferGeometry, Group, Texture } from 'three'
import { validateGlb } from './glb'
import { disposeModel, parseModel } from './three-model'

function fixture(change: (json: Record<string, unknown>) => void = () => {}, binary = new Uint8Array(new Float32Array([-1, -1, 0, 1, -1, 0, 0, 1, 0]).buffer)) {
  const json: Record<string, unknown> = {
    asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    buffers: [{ byteLength: binary.length }], bufferViews: [{ buffer: 0, byteLength: 36 }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 3, type: 'VEC3', min: [-1, -1, 0], max: [1, 1, 0] }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0 } }] }],
  }
  change(json)
  const source = new TextEncoder().encode(JSON.stringify(json)), jsonLength = Math.ceil(source.length / 4) * 4
  const result = new ArrayBuffer(28 + jsonLength + Math.ceil(binary.length / 4) * 4), view = new DataView(result)
  view.setUint32(0, 0x46546c67, true); view.setUint32(4, 2, true); view.setUint32(8, result.byteLength, true)
  view.setUint32(12, jsonLength, true); view.setUint32(16, 0x4e4f534a, true)
  const bytes = new Uint8Array(result); bytes.fill(32, 20, 20 + jsonLength); bytes.set(source, 20)
  view.setUint32(20 + jsonLength, result.byteLength - jsonLength - 28, true); view.setUint32(24 + jsonLength, 0x004e4942, true)
  bytes.set(binary, 28 + jsonLength)
  return result
}

describe('bounded GLB preview', () => {
  it('parses genuine geometry without network access', async () => {
    const fetch = vi.spyOn(globalThis, 'fetch')
    const buffer = fixture()
    expect(validateGlb(buffer)).toMatchObject({ meshes: 1, vertices: 3 })
    const gltf = await parseModel(buffer)
    const mesh = gltf.scene.children[0] as Mesh
    expect(mesh.geometry.attributes.position.count).toBe(3)
    expect(fetch).not.toHaveBeenCalled()
    disposeModel(gltf.scene); fetch.mockRestore()
  })
  it.each(['https://example.com/texture.png', 'data:application/octet-stream;base64,AA==', '../other.bin', 'blob:foreign'])('rejects %s before parser requests', async uri => {
    const buffer = fixture(json => { json.images = [{ uri }] })
    const fetch = vi.spyOn(globalThis, 'fetch')
    await expect(parseModel(buffer)).rejects.toThrow('URI')
    expect(fetch).not.toHaveBeenCalled(); fetch.mockRestore()
  })
  it('rejects bad headers, truncation and excessive files', () => {
    const valid = fixture(), wrong = valid.slice(0)
    new DataView(wrong).setUint32(8, 1, true)
    expect(() => validateGlb(wrong)).toThrow('2.0')
    expect(() => validateGlb(valid.slice(0, -4))).toThrow()
    expect(() => validateGlb(new ArrayBuffer(20 * 1024 * 1024 + 1))).toThrow('20 MiB')
  })
  it('rejects decoded geometry overflow, sparse and compressed extensions', () => {
    expect(() => validateGlb(fixture(json => { json.accessors = [{ bufferView: 0, componentType: 5126, count: 200001, type: 'VEC3' }] }))).toThrow()
    expect(() => validateGlb(fixture(json => { json.accessors = [{ bufferView: 0, sparse: {}, componentType: 5126, count: 3, type: 'VEC3' }] }))).toThrow('sparse')
    expect(() => validateGlb(fixture(json => { json.extensionsUsed = ['KHR_draco_mesh_compression'] }))).toThrow('扩展')
  })
  it('rejects cycles, repeated parents and excessive draw calls', () => {
    expect(() => validateGlb(fixture(json => { json.nodes = [{ children: [0], mesh: 0 }] }))).toThrow()
    expect(() => validateGlb(fixture(json => { json.nodes = [{ children: [2], mesh: 0 }, { children: [2] }, {}] }))).toThrow('父级')
    expect(() => validateGlb(fixture(json => { json.meshes = Array.from({ length: 10 }, () => ({ primitives: Array.from({ length: 32 }, () => ({ attributes: { POSITION: 0 } })) })) }))).toThrow('批次')
  })
  it('rejects oversized textures before decoding', () => {
    const binary = new Uint8Array(72), view = new DataView(binary.buffer)
    view.setUint32(36, 0x89504e47); view.setUint32(40, 0x0d0a1a0a); view.setUint32(44, 13); view.setUint32(48, 0x49484452)
    view.setUint32(52, 8192); view.setUint32(56, 8192)
    expect(() => validateGlb(fixture(json => {
      json.bufferViews = [{ buffer: 0, byteLength: 36 }, { buffer: 0, byteOffset: 36, byteLength: 36 }]
      json.images = [{ bufferView: 1, mimeType: 'image/png' }]
    }, binary))).toThrow('纹理尺寸')
  })
  it('keeps rig support exclusive to the formal mascot loader', () => {
    const buffer = fixture(json => { json.skins = [{ joints: [0] }] })
    expect(() => validateGlb(buffer)).toThrow('蒙皮')
    expect(validateGlb(buffer, true).vertices).toBe(3)
  })
  it('disposes shared resources once within the owned scene', () => {
    const geometry = new BufferGeometry(), material = new MeshStandardMaterial(), texture = new Texture()
    material.map = texture
    const root = new Group(); root.add(new Mesh(geometry, material), new Mesh(geometry, material))
    const geometryDispose = vi.spyOn(geometry, 'dispose'), materialDispose = vi.spyOn(material, 'dispose'), textureDispose = vi.spyOn(texture, 'dispose')
    disposeModel(root)
    expect(geometryDispose).toHaveBeenCalledTimes(1); expect(materialDispose).toHaveBeenCalledTimes(1); expect(textureDispose).toHaveBeenCalledTimes(1)
  })
})
