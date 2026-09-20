/** Bounded, self-contained GLB 2.0. Validation happens before Three's parser or image decoding. */
export interface GlbInfo { meshes: number; vertices: number; bytes: number }
type RecordValue = Record<string, unknown>
const record = (value: unknown): RecordValue => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('GLB 对象结构无效')
  return value as RecordValue
}
const integer = (value: unknown, max: number, fallback?: number): number => {
  if (value === undefined && fallback !== undefined) return fallback
  if (!Number.isSafeInteger(value) || (value as number) < 0 || (value as number) > max) throw new Error('GLB 数量或范围超出预览限制')
  return value as number
}
const list = (value: unknown, max: number): unknown[] => {
  if (value === undefined) return []
  if (!Array.isArray(value) || value.length > max) throw new Error('GLB 对象数量超出预览限制')
  return value
}

function imageDimensions(bytes: Uint8Array, mime: unknown): [number, number] {
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength)
  if (mime === 'image/png' && bytes.length >= 33 && view.getUint32(0) === 0x89504e47
    && view.getUint32(4) === 0x0d0a1a0a && view.getUint32(8) === 13 && view.getUint32(12) === 0x49484452) {
    return [view.getUint32(16), view.getUint32(20)]
  }
  if (mime === 'image/jpeg' && bytes.length > 4 && view.getUint16(0) === 0xffd8) {
    let offset = 2
    while (offset + 4 <= bytes.length) {
      if (bytes[offset] !== 0xff) break
      const marker = bytes[offset + 1]
      if (marker === 0xff) { offset++; continue }
      const size = view.getUint16(offset + 2)
      if (size < 2 || offset + 2 + size > bytes.length) break
      if ([0xc0, 0xc1, 0xc2].includes(marker) && size >= 8) return [view.getUint16(offset + 7), view.getUint16(offset + 5)]
      if (marker === 0xda || marker === 0xd9) break
      offset += size + 2
    }
  }
  throw new Error('仅支持有效的内嵌 PNG/JPEG 纹理')
}

export function validateGlb(buffer: ArrayBuffer, allowRig = false): GlbInfo {
  if (buffer.byteLength < 28 || buffer.byteLength > 20 * 1024 * 1024) throw new Error('GLB 文件为空、截断或超过 20 MiB')
  const view = new DataView(buffer)
  if (view.getUint32(0, true) !== 0x46546c67 || view.getUint32(4, true) !== 2
    || view.getUint32(8, true) !== buffer.byteLength) throw new Error('不是有效的 GLB 2.0 文件')
  const jsonLength = view.getUint32(12, true)
  if (jsonLength > 1024 * 1024 || jsonLength % 4 || 20 + jsonLength + 8 > buffer.byteLength
    || view.getUint32(16, true) !== 0x4e4f534a) throw new Error('GLB JSON 区块无效或过大')
  const binHeader = 20 + jsonLength, binStart = binHeader + 8
  const binLength = view.getUint32(binHeader, true)
  if (binLength % 4 || view.getUint32(binHeader + 4, true) !== 0x004e4942 || binStart + binLength !== buffer.byteLength) throw new Error('GLB 二进制区块无效')
  let json: RecordValue
  try { json = record(JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 20, jsonLength)))) }
  catch { throw new Error('GLB JSON 无法解析') }
  let properties = 0
  const inspect = (value: unknown, depth: number) => {
    if (depth > 32 || ++properties > 100_000) throw new Error('GLB 结构过于复杂')
    if (typeof value === 'number' && !Number.isFinite(value)) throw new Error('GLB 数值无效')
    if (value && typeof value === 'object') for (const [key, child] of Object.entries(value)) {
      if (key === 'uri') throw new Error('预览不允许模型请求外部或 data URI 资源，请使用内嵌 GLB')
      if (key === 'extensions' && Object.keys(record(child)).some(name => name !== 'KHR_materials_unlit')) throw new Error('模型扩展尚不支持')
      inspect(child, depth + 1)
    }
  }
  inspect(json, 0)
  if (record(json.asset).version !== '2.0') throw new Error('仅支持 glTF 2.0')
  if (list(json.extensionsUsed, 32).some(value => value !== 'KHR_materials_unlit')
    || list(json.extensionsRequired, 32).some(value => value !== 'KHR_materials_unlit')) throw new Error('模型包含尚不支持的压缩或材质扩展')
  const skins = list(json.skins, 64)
  if (!allowRig && skins.length) throw new Error('首版预览暂不支持蒙皮模型')
  for (const skin of skins) list(record(skin).joints, 512)
  const buffers = list(json.buffers, 1)
  if (buffers.length !== 1) throw new Error('GLB 必须包含单个内嵌 buffer')
  const declaredBytes = integer(record(buffers[0]).byteLength, binLength)
  const views = list(json.bufferViews, 512).map(value => {
    const item = record(value)
    if (integer(item.buffer, 0) !== 0) throw new Error('GLB buffer 无效')
    const start = integer(item.byteOffset, declaredBytes, 0), length = integer(item.byteLength, declaredBytes)
    if (start + length > declaredBytes) throw new Error('GLB bufferView 越界')
    return { start, length, stride: integer(item.byteStride, 252, 0) }
  })
  let decodedBytes = 0
  const accessors = list(json.accessors, 512).map(value => {
    const item = record(value)
    if (item.sparse) throw new Error('首版预览暂不支持 sparse accessor')
    const source = views[integer(item.bufferView, views.length - 1)]
    const componentBytes = ({ 5120: 1, 5121: 1, 5122: 2, 5123: 2, 5125: 4, 5126: 4 } as Record<string, number>)[String(item.componentType)]
    const components = ({ SCALAR: 1, VEC2: 2, VEC3: 3, VEC4: 4, MAT4: 16 } as Record<string, number>)[String(item.type)]
    if (!source || !componentBytes || !components) throw new Error('GLB accessor 类型无效')
    const count = integer(item.count, 600_000), offset = integer(item.byteOffset, source.length, 0)
    const size = components * componentBytes, stride = source.stride || size
    if (!count || stride < size || stride % componentBytes || offset % componentBytes
      || offset + (count - 1) * stride + size > source.length) throw new Error('GLB accessor 越界或对齐无效')
    decodedBytes += count * size
    if (decodedBytes > 32 * 1024 * 1024) throw new Error('模型解码规模超过 32 MiB')
    return { count, type: item.type, componentType: item.componentType }
  })
  let vertices = 0
  let primitiveCount = 0
  const meshes = list(json.meshes, 128).map(mesh => {
    let count = 0
    for (const value of list(record(mesh).primitives, 32)) {
      if (++primitiveCount > 256) throw new Error('模型绘制批次超过预览限制')
      const primitive = record(value)
      if (primitive.targets) throw new Error('首版预览暂不支持 morph targets')
      const position = accessors[integer(record(primitive.attributes).POSITION, accessors.length - 1)]
      if (!position || position.type !== 'VEC3') throw new Error('模型缺少有效顶点')
      for (const attribute of Object.values(record(primitive.attributes))) integer(attribute, accessors.length - 1)
      if (primitive.indices !== undefined) {
        const indices = accessors[integer(primitive.indices, accessors.length - 1)]
        if (indices.type !== 'SCALAR' || ![5121, 5123, 5125].includes(Number(indices.componentType))) throw new Error('模型索引类型无效')
      }
      if (primitive.mode !== undefined) integer(primitive.mode, 6)
      if (primitive.material !== undefined) integer(primitive.material, list(json.materials, 128).length - 1)
      count += position.count
    }
    vertices += count
    if (vertices > 200_000) throw new Error('模型顶点超过 20 万')
    return count
  })
  if (!meshes.length || !vertices) throw new Error('GLB 没有可预览的网格')
  const nodes = list(json.nodes, 512).map(record)
  const scenes = list(json.scenes, 1)
  if (scenes.length !== 1 || integer(json.scene, 0, 0) !== 0) throw new Error('首版仅支持单场景 GLB')
  const roots = list(record(scenes[0]).nodes, 512).map(value => integer(value, nodes.length - 1))
  if (new Set(roots).size !== roots.length) throw new Error('模型场景存在重复节点')
  let renderedVertices = 0
  const parents = new Set<number>()
  const visit = (index: number, path: Set<number>, depth: number) => {
    if (depth > 32 || path.has(index)) throw new Error('模型节点循环或层级过深')
    const node = nodes[index]
    if (!node) throw new Error('模型节点不存在')
    if (node.mesh !== undefined) renderedVertices += meshes[integer(node.mesh, meshes.length - 1)]
    if (renderedVertices > 400_000) throw new Error('模型实例顶点超过 40 万')
    const next = new Set(path); next.add(index)
    for (const child of list(node.children, 64)) {
      const id = integer(child, nodes.length - 1)
      if (parents.has(id)) throw new Error('模型节点存在重复父级')
      parents.add(id); visit(id, next, depth + 1)
    }
  }
  for (const [index, node] of nodes.entries()) for (const child of list(node.children, 64)) {
    integer(child, nodes.length - 1)
    if (child === index) throw new Error('模型节点自引用')
  }
  const children = new Set(nodes.flatMap(node => list(node.children, 64) as number[]))
  if (roots.some(root => children.has(root))) throw new Error('场景根节点存在父级')
  for (let index = 0; index < nodes.length; index++) if (!children.has(index)) visit(index, new Set(), 0)
  if (parents.size + nodes.filter((_, index) => !children.has(index)).length !== nodes.length) throw new Error('模型节点图无效')
  let pixels = 0
  for (const value of list(json.images, 16)) {
    const image = record(value), source = views[integer(image.bufferView, views.length - 1)]
    if (!source) throw new Error('纹理 bufferView 无效')
    const [width, height] = imageDimensions(new Uint8Array(buffer, binStart + source.start, source.length), image.mimeType)
    pixels += width * height
    if (!width || !height || width > 2048 || height > 2048 || pixels > 8_388_608) throw new Error('纹理尺寸超过预览限制')
  }
  list(json.materials, 128)
  for (const texture of list(json.textures, 32)) integer(record(texture).source, list(json.images, 16).length - 1)
  for (const skin of skins) {
    for (const joint of list(record(skin).joints, 512)) integer(joint, nodes.length - 1)
    if (record(skin).inverseBindMatrices !== undefined) integer(record(skin).inverseBindMatrices, accessors.length - 1)
  }
  for (const animation of list(json.animations, 8)) {
    const clip = record(animation), samplers = list(clip.samplers, 512)
    for (const sampler of samplers) {
      integer(record(sampler).input, accessors.length - 1); integer(record(sampler).output, accessors.length - 1)
    }
    for (const channel of list(clip.channels, 512)) {
      integer(record(channel).sampler, samplers.length - 1)
      const target = record(record(channel).target)
      integer(target.node, nodes.length - 1)
      if (!['translation', 'rotation', 'scale'].includes(String(target.path))) throw new Error('动画通道尚不支持')
    }
  }
  return { meshes: meshes.length, vertices, bytes: buffer.byteLength }
}
