import { useEffect, useState } from 'react'
import type { MascotGesture, MascotMotionMode } from './mascot-types'
import styles from './mascot.module.css'

const COMPLETE_POSE_SOURCES = {
  idle: '/lumen-fox-complete-idle-9tail.png',
  alert: '/lumen-fox-complete-alert.png',
  stretch: '/lumen-fox-complete-stretch.png',
  submit: '/lumen-fox-complete-wave-9tail.png',
} satisfies Record<MascotGesture, string>

/** 与 mascot.module.css 中 authoredPose 的过渡时长对齐，用于退场后卸载旧 pose */
const POSE_FADE_OUT_MS = 520

interface RasterMascotRendererProps {
  gesture: MascotGesture
  focused: boolean
  blinking: boolean
  motionMode: MascotMotionMode
  active: boolean
}

export function RasterMascotRenderer({
  gesture,
  focused,
  blinking,
  motionMode,
  active,
}: RasterMascotRendererProps) {
  const canMove = motionMode === 'full' && active
  const canBreathe = canMove && gesture === 'idle'
  const gestureClass = gesture === 'alert' || gesture === 'stretch'
    ? styles.gestureSettle
    : gesture === 'submit' ? styles.gestureSubmit : ''
  const authoredPose: MascotGesture | null = gesture !== 'idle'
    ? gesture
    : motionMode === 'static' ? 'idle' : null

  // 按需挂载 pose 大图：只挂载当前 pose，切换时保留上一张直到淡出结束，
  // 新图先以未激活态挂载、下一帧再加激活 class，保持既有 crossfade 体验
  const [currentPose, setCurrentPose] = useState(authoredPose)
  const [exitingPose, setExitingPose] = useState<MascotGesture | null>(null)
  const [poseEntered, setPoseEntered] = useState(authoredPose === null)

  useEffect(() => {
    if (currentPose === authoredPose) return
    setExitingPose(currentPose)
    setCurrentPose(authoredPose)
    setPoseEntered(false)
  }, [authoredPose, currentPose])

  useEffect(() => {
    if (poseEntered || currentPose === null) return
    const frame = requestAnimationFrame(() => setPoseEntered(true))
    return () => cancelAnimationFrame(frame)
  }, [currentPose, poseEntered])

  useEffect(() => {
    if (!exitingPose) return
    const timer = window.setTimeout(() => setExitingPose(null), POSE_FADE_OUT_MS)
    return () => window.clearTimeout(timer)
  }, [exitingPose])

  const renderedPoses: { pose: MascotGesture; active: boolean }[] = []
  if (currentPose) renderedPoses.push({ pose: currentPose, active: poseEntered })
  if (exitingPose && exitingPose !== currentPose) renderedPoses.push({ pose: exitingPose, active: false })

  return (
    <div
      className={styles.visual}
      data-gesture={gesture}
      data-pose={authoredPose ?? 'layered-idle'}
      data-motion={motionMode}
      data-active={active}
      data-channels={`tail:${canMove ? 'play' : 'stop'};balance:${canMove ? 'play' : 'stop'};breath:${canBreathe ? 'play' : 'stop'};look:${focused ? 'focused' : 'idle'};blink:${blinking ? 'closed' : 'open'}`}
    >
      <span className={styles.aura} />
      <span className={`${styles.shadow} ${gestureClass}`} />
      <div className={`${styles.rig} ${gestureClass}`}>
        <div className={`${styles.layeredCharacter} ${authoredPose ? styles.layeredCharacterHidden : ''}`}>
          <img
            className={`${styles.tail} ${canMove ? styles.tailMotion : ''}`}
            src="/lumen-fox-tail-fan.webp"
            alt=""
            draggable={false}
          />
          <div className={`${styles.bodyRig} ${canMove ? styles.bodyLife : ''}`}>
            <img
              className={styles.body}
              src="/lumen-fox-body-idle.webp"
              alt=""
              draggable={false}
            />
            <img
              className={`${styles.localLayer} ${styles.breathLayer} ${canBreathe ? styles.breathActive : ''}`}
              src="/lumen-fox-body-idle.webp"
              alt=""
              draggable={false}
            />
            <img
              className={`${styles.localLayer} ${styles.focusLayer} ${focused ? styles.localLayerActive : ''}`}
              src="/lumen-fox-body-idle.webp"
              alt=""
              draggable={false}
            />
            <img
              className={`${styles.localLayer} ${styles.blinkLayer} ${blinking ? styles.localLayerActive : ''}`}
              src="/lumen-fox-body-blink.webp"
              alt=""
              draggable={false}
            />
          </div>
        </div>
        {renderedPoses.map(({ pose, active: poseActive }) => (
          <img
            key={pose}
            className={`${styles.authoredPose} ${poseActive ? styles.authoredPoseActive : ''}`}
            src={COMPLETE_POSE_SOURCES[pose]}
            alt=""
            decoding="async"
            draggable={false}
          />
        ))}
      </div>
    </div>
  )
}
