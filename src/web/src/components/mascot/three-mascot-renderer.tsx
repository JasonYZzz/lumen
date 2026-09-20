'use client'

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useFrame } from '@react-three/fiber'
import { AnimationMixer, Box3, Group, LoopOnce, Vector3, type AnimationClip, type Object3D } from 'three'
import { disposeModel, parseModel } from '@/lib/three-model'
import { SceneCanvas } from '../scene-canvas'
import type { MascotActivity } from './mascot-types'

export function mascotModelPath(value: string | undefined): string | null {
  return value && /^\/mascot\/[a-z\d_./-]+\.glb$/i.test(value) && !value.split('/').includes('..') ? value : null
}

function AnimatedModel({ model, clips, activity, interaction }: {
  model: Object3D; clips: AnimationClip[]; activity: MascotActivity; interaction: number
}) {
  const mixer = useMemo(() => new AnimationMixer(model), [model])
  useEffect(() => {
    const idle = clips.find(clip => /^idle$/i.test(clip.name)) ?? clips[0]
    const react = clips.find(clip => /^(react|reaction|wave)$/i.test(clip.name))
    const idleAction = idle ? mixer.clipAction(idle) : null
    idleAction?.play()
    if (react && (interaction > 0 || activity !== 'idle')) {
      const reaction = mixer.clipAction(react).reset().setLoop(LoopOnce, 1)
      reaction.clampWhenFinished = true
      idleAction?.stop(); reaction.play()
      const finished = () => { reaction.stop(); idleAction?.reset().play() }
      mixer.addEventListener('finished', finished)
      return () => { mixer.removeEventListener('finished', finished); mixer.stopAllAction(); mixer.uncacheRoot(model) }
    }
    return () => { mixer.stopAllAction(); mixer.uncacheRoot(model) }
  }, [mixer, clips, model, activity, interaction])
  useFrame((_state, delta) => mixer.update(Math.min(delta, .1)))
  return <primitive object={model} dispose={null} />
}

/** Locally authored GLB, with the existing video retained as the failure fallback. */
export function ThreeMascotRenderer({ url, active, activity, interaction, onUnavailable }: {
  url: string; active: boolean; activity: MascotActivity; interaction: number; onUnavailable: () => void
}) {
  const [asset, setAsset] = useState<{ model: Object3D; clips: AnimationClip[] } | null>(null)
  const fail = useCallback(() => onUnavailable(), [onUnavailable])
  useEffect(() => {
    const abort = new AbortController()
    let owned: Object3D | null = null
    const load = async () => {
      if (!mascotModelPath(url)) throw new Error('角色模型必须位于本地 /mascot/ 目录')
      const response = await fetch(url, { signal: abort.signal })
      if (!response.ok) throw new Error('角色模型不可用')
      if (Number(response.headers.get('content-length')) > 20 * 1024 * 1024) throw new Error('角色模型过大')
      const gltf = await parseModel(await response.arrayBuffer(), true)
      owned = gltf.scene
      if (abort.signal.aborted) { disposeModel(owned); owned = null; return }
      const bounds = new Box3().setFromObject(owned), size = bounds.getSize(new Vector3()).length()
      if (!Number.isFinite(size) || size <= 0) throw new Error('角色模型边界无效')
      owned.position.sub(bounds.getCenter(new Vector3()))
      const group = new Group(); group.add(owned); group.scale.setScalar(2.5 / size)
      setAsset({ model: group, clips: gltf.animations })
    }
    void load().catch(() => { if (!abort.signal.aborted) fail() })
    return () => { abort.abort(); if (owned) { disposeModel(owned); owned = null } }
  }, [url, fail])
  if (!asset) return <img src="/mascot/fox-poster.png" alt="" width={168} height={168} />
  return <SceneCanvas active={active} animate={asset.clips.length > 0} onFailure={fail}
    fallback={<img src="/mascot/fox-poster.png" alt="" width={168} height={168} />}>
    <ambientLight intensity={1.5} /><directionalLight position={[3, 4, 5]} intensity={2} />
    <AnimatedModel model={asset.model} clips={asset.clips} activity={activity} interaction={interaction} />
  </SceneCanvas>
}
