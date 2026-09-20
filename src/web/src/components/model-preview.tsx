'use client'

import { OrbitControls } from '@react-three/drei'
import { useCallback, useEffect, useRef, useState, type ComponentRef } from 'react'
import { Box3, Group, Vector3, type Object3D } from 'three'
import { parseModel, disposeModel } from '@/lib/three-model'
import { useMascotVisibility } from './mascot/mascot-visibility'
import { SceneCanvas } from './scene-canvas'

export function ModelPreview({ buffer }: { buffer: ArrayBuffer }) {
  const stage = useRef<HTMLDivElement>(null), controls = useRef<ComponentRef<typeof OrbitControls>>(null)
  const visible = useMascotVisibility(stage)
  const [model, setModel] = useState<Object3D | null>(null)
  const [error, setError] = useState('')
  const [attempt, setAttempt] = useState(0)
  const fail = useCallback(() => setError('3D 渲染不可用；仍可下载原文件。'), [])
  useEffect(() => {
    let disposed = false, owned: Object3D | null = null
    setModel(null); setError('')
    void parseModel(buffer).then(gltf => {
      owned = gltf.scene
      if (disposed) { disposeModel(owned); owned = null; return }
      const bounds = new Box3().setFromObject(owned), size = bounds.getSize(new Vector3()).length()
      if (!Number.isFinite(size) || size <= 0) throw new Error('模型边界无效')
      const center = bounds.getCenter(new Vector3())
      const group = new Group(); group.add(owned)
      owned.position.sub(center); group.scale.setScalar(2.5 / size)
      setModel(group)
    }).catch(cause => {
      if (owned) { disposeModel(owned); owned = null }
      if (!disposed) setError(cause instanceof Error ? cause.message : '模型无法解析')
    })
    return () => { disposed = true; if (owned) { disposeModel(owned); owned = null } }
  }, [buffer, attempt])
  const turn = (direction: number) => {
    const orbit = controls.current
    if (!orbit) return
    const offset = orbit.object.position.clone().sub(orbit.target).applyAxisAngle(new Vector3(0, 1, 0), direction * Math.PI / 8)
    orbit.object.position.copy(orbit.target).add(offset); orbit.update()
  }
  const zoom = (factor: number) => {
    const orbit = controls.current
    if (!orbit) return
    const offset = orbit.object.position.clone().sub(orbit.target)
    const distance = Math.max(1.5, Math.min(9, offset.length() * factor))
    orbit.object.position.copy(orbit.target).add(offset.setLength(distance)); orbit.update()
  }
  return <div className="model-preview" ref={stage}>
    <div className="model-preview-stage">
      {error ? <div className="scene-message" role="alert"><p>{error}</p><button type="button" onClick={() => setAttempt(attempt + 1)}>重新预览</button></div>
        : !model ? <p className="scene-message" role="status">正在解析 3D 模型…</p>
        : <SceneCanvas key={attempt} active={visible} camera={[3, 2, 4]} onFailure={fail} fallback={<p className="scene-message">3D 渲染不可用；请下载原文件。</p>}>
          <ambientLight intensity={1.5} /><directionalLight position={[3, 5, 4]} intensity={2.5} />
          <primitive object={model} dispose={null} />
          <OrbitControls ref={controls} enableDamping={false} enablePan={false} minDistance={1.5} maxDistance={9} />
        </SceneCanvas>}
    </div>
    <div className="model-preview-toolbar" aria-label="模型视角">
      <button type="button" disabled={!model || Boolean(error)} onClick={() => turn(-1)}>向左旋转</button>
      <button type="button" disabled={!model || Boolean(error)} onClick={() => turn(1)}>向右旋转</button>
      <button type="button" disabled={!model || Boolean(error)} onClick={() => zoom(.8)}>放大</button>
      <button type="button" disabled={!model || Boolean(error)} onClick={() => zoom(1.25)}>缩小</button>
      <button type="button" disabled={!model || Boolean(error)} onClick={() => controls.current?.reset()}>重置视角</button>
    </div>
    <p className="document-preview-note">静态 GLB 预览 · 拖动旋转，滚轮缩放；外部资源与压缩扩展不加载。</p>
  </div>
}
