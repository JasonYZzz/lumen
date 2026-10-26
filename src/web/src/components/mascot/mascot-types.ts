export type MascotActivity = 'idle' | 'focused' | 'submitting'

export type MascotMotionMode = 'full' | 'reduced' | 'static'

export type MascotGesture = 'idle' | 'alert' | 'stretch' | 'submit'

export interface MascotSceneProps {
  activity: MascotActivity
  stage?: number
  motionMode?: MascotMotionMode
  className?: string
}
