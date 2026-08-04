import type { MascotGesture, MascotMotionMode } from './mascot-types'
import styles from './mascot.module.css'

const COMPLETE_POSE_SOURCES = {
  idle: '/lumen-fox-complete-idle-9tail.png',
  alert: '/lumen-fox-complete-alert.png',
  stretch: '/lumen-fox-complete-stretch.png',
  submit: '/lumen-fox-complete-wave-9tail.png',
} satisfies Record<MascotGesture, string>

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
  const authoredPose = gesture !== 'idle'
    ? gesture
    : motionMode === 'static' ? 'idle' : null

  return (
    <div
      className={styles.visual}
      data-gesture={gesture}
      data-pose={authoredPose ?? 'layered-idle'}
      data-motion={motionMode}
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
        {(Object.entries(COMPLETE_POSE_SOURCES) as [MascotGesture, string][]).map(([pose, source]) => (
          <img
            key={pose}
            className={`${styles.authoredPose} ${authoredPose === pose ? styles.authoredPoseActive : ''}`}
            src={source}
            alt=""
            draggable={false}
          />
        ))}
      </div>
    </div>
  )
}
