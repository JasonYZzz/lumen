'use client'

import { useFrame } from '@react-three/fiber'
import { useRef, useState, useCallback, type RefObject } from 'react'
import type { Mesh } from 'three'
import type { AudioLevels } from '@/lib/live/realtime-client'
import type { LiveUiStatus } from '@/lib/live/live-reducer'
import { SceneCanvas } from './scene-canvas'

function VoiceShape({ levels, status, muted }: { levels: RefObject<AudioLevels>; status: LiveUiStatus; muted: boolean }) {
  const shape = useRef<Mesh>(null)
  useFrame((_state, delta) => {
    if (!shape.current) return
    const responding = status === 'assistant_speaking'
    const listening = ['listening', 'user_speaking'].includes(status)
    if (!responding && !listening) { shape.current.scale.setScalar(1); return }
    const level = responding ? levels.current.output : listening && !muted ? levels.current.input : 0
    const target = 1 + Math.min(level * 1.8, 0.22)
    const blend = 1 - Math.exp(-Math.min(delta, .1) * 10)
    const scale = shape.current.scale.x + (target - shape.current.scale.x) * blend
    shape.current.scale.setScalar(scale)
  })
  const waiting = ['approval_pending', 'error', 'reconnecting'].includes(status)
  return <mesh ref={shape} rotation={[.15, .3, 0]}>
    <icosahedronGeometry args={[1, 3]} />
    <meshStandardMaterial color={waiting ? '#9b9486' : status === 'assistant_speaking' ? '#b77035' : '#d8b685'}
      roughness={.38} metalness={.18} flatShading />
  </mesh>
}

export function VoiceScene({ levels, status, muted, active, onUnavailable }: {
  levels: RefObject<AudioLevels>; status: LiveUiStatus; muted: boolean; active: boolean; onUnavailable?: () => void
}) {
  const [failed, setFailed] = useState(false)
  const fail = useCallback(() => { setFailed(true); onUnavailable?.() }, [onUnavailable])
  const fallback = <div className="voice-static-shape" aria-hidden="true" />
  if (failed) return fallback
  return <SceneCanvas active={active} animate={active && ['listening', 'user_speaking', 'assistant_speaking'].includes(status)}
    onFailure={fail} fallback={fallback}>
    <ambientLight intensity={1.3} />
    <directionalLight position={[3, 4, 5]} intensity={2.6} />
    <directionalLight position={[-4, 0, 2]} intensity={.5} />
    <VoiceShape levels={levels} status={status} muted={muted} />
  </SceneCanvas>
}
