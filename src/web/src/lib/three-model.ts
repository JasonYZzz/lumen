import { GLTFLoader, type GLTF } from 'three/addons/loaders/GLTFLoader.js'
import { LoadingManager, Mesh, SkinnedMesh, Texture, type Material, type Object3D } from 'three'
import { validateGlb } from './glb'

/** Each call owns a fresh model. No global loader cache or network decoder. */
export async function parseModel(buffer: ArrayBuffer, allowRig = false): Promise<GLTF> {
  validateGlb(buffer, allowRig)
  const manager = new LoadingManager()
  manager.setURLModifier(url => {
    // All JSON URIs were rejected; only GLTFLoader's embedded image object URLs remain.
    if (!url.startsWith('blob:')) throw new Error('模型资源必须内嵌')
    return url
  })
  return new GLTFLoader(manager).parseAsync(buffer, '')
}

export function disposeModel(root: Object3D) {
  const geometries = new Set<Mesh['geometry']>(), materials = new Set<Material>(), textures = new Set<Texture>()
  root.traverse(object => {
    if (!(object instanceof Mesh)) return
    geometries.add(object.geometry)
    for (const material of Array.isArray(object.material) ? object.material : [object.material]) materials.add(material)
    if (object instanceof SkinnedMesh) object.skeleton.dispose()
  })
  for (const material of materials) {
    for (const value of Object.values(material)) if (value instanceof Texture) textures.add(value)
    material.dispose()
  }
  const images = new Set<ImageBitmap>()
  for (const texture of textures) {
    if (typeof ImageBitmap !== 'undefined' && texture.image instanceof ImageBitmap) images.add(texture.image)
    texture.dispose()
  }
  images.forEach(image => image.close()); geometries.forEach(geometry => geometry.dispose())
}
