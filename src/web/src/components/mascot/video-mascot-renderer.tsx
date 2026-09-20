'use client'

import { useEffect, useRef, useState } from 'react'
import { MascotPlayback, type MascotClip } from './mascot-playback'
import type { MascotActivity, MascotMotionMode } from './mascot-types'
import styles from './mascot.module.css'

const SOURCES: Record<MascotClip, string> = {
  idle: '/mascot/fox-idle.alpha.mp4',
  react: '/mascot/fox-react.alpha.mp4',
}

// The contour follows the authored alpha, so it never distorts the character or invents its shape.
const FRAGMENT_SHADER = `
precision mediump float;
varying vec2 uv;
uniform sampler2D film;
uniform vec2 maskStep;
uniform float elapsed;
uniform float focus;
uniform float reaction;
float mask(vec2 point) {
  vec2 bounded = clamp(point, vec2(0.001), vec2(0.999));
  return texture2D(film, vec2(0.5 + bounded.x * 0.5, bounded.y)).r;
}
void main() {
  vec3 color = texture2D(film, vec2(uv.x * 0.5, uv.y)).rgb;
  float alpha = mask(uv);
  float outer = max(max(mask(uv + vec2(maskStep.x, 0.0)), mask(uv - vec2(maskStep.x, 0.0))),
                    max(mask(uv + vec2(0.0, maskStep.y)), mask(uv - vec2(0.0, maskStep.y))));
  float contour = max(outer - alpha, 0.0);
  float sweep = 0.5 + 0.5 * sin(uv.y * 7.0 + uv.x * 3.0 - elapsed * 0.65);
  float strength = mix(0.16, 0.05, focus) + reaction * 0.16;
  float trace = contour * strength * (0.35 + sweep * 0.65);
  vec3 amber = vec3(0.80, 0.53, 0.24);
  gl_FragColor = vec4(color * alpha + amber * trace * (1.0 - alpha), alpha + trace * (1.0 - alpha));
}`

interface VideoMascotRendererProps {
  activity: MascotActivity
  motionMode: MascotMotionMode
  active: boolean
  paused?: boolean
  playbackRate?: number
  interaction?: number
}

/** H.264 RGB + alpha side-by-side: browser video decoding, local alpha contour in the same canvas.
 * No per-frame React renders, CPU pixel readbacks, pose PNG swaps or green-screen spill.
 * The actual movement lives in the authored clips, not in CSS/vertex deformation.
 */
export function VideoMascotRenderer({
  activity, motionMode, active, paused = false, playbackRate = 1, interaction = 0,
}: VideoMascotRendererProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const live = useRef({ activity, motionMode, active, paused, playbackRate })
  const controller = useRef<{ sync: () => void; react: () => void } | null>(null)
  const [ready, setReady] = useState(false)
  const [failed, setFailed] = useState(false)
  live.current = { activity, motionMode, active, paused, playbackRate }

  useEffect(() => controller.current?.sync(), [activity, motionMode, active, paused, playbackRate])
  useEffect(() => { if (interaction) controller.current?.react() }, [interaction])

  useEffect(() => {
    setReady(false); setFailed(false)
    // Reduced/static modes do not download or decode video at all.
    if (motionMode !== 'full') { setReady(false); return }
    const canvas = canvasRef.current!
    const gl = canvas.getContext('webgl', { alpha: true, premultipliedAlpha: true, antialias: false, depth: false, powerPreference: 'low-power' })
    if (!gl) { setFailed(true); return }
    let disposed = false, broken = false, announced = false, callback = 0, raf = 0, playVersion = 0
    let cancelWarmup: (() => void) | null = null
    let lastFrame = -Infinity, lastDraw: number | null = null, elapsed = 0, focus = 0, reaction = 0
    let callbackOwner: HTMLVideoElement | null = null
    const playback = new MascotPlayback()
    const shaders: WebGLShader[] = []
    const program = gl.createProgram()!
    const texture = gl.createTexture()!
    const buffer = gl.createBuffer()!
    const videos = {} as Record<MascotClip, HTMLVideoElement>
    let maskStepUniform: WebGLUniformLocation | null = null
    let elapsedUniform: WebGLUniformLocation | null = null
    let focusUniform: WebGLUniformLocation | null = null
    let reactionUniform: WebGLUniformLocation | null = null
    const compact = window.matchMedia?.('(max-width: 640px)')
    const canPlay = () => live.current.active && !live.current.paused && live.current.motionMode === 'full'
    const loadClip = (clip: MascotClip) => {
      const video = videos[clip]
      if (video.hasAttribute('src')) return
      video.preload = 'auto'
      video.src = SOURCES[clip]
      video.load()
    }
    const warmReaction = () => {
      if (cancelWarmup || videos.react.hasAttribute('src') || !canPlay()) return
      const warm = () => {
        cancelWarmup = null
        if (!disposed && !broken && canPlay()) loadClip('react')
      }
      if (typeof window.requestIdleCallback === 'function') {
        const id = window.requestIdleCallback(warm, { timeout: 1_500 })
        cancelWarmup = () => window.cancelIdleCallback(id)
      } else {
        const id = window.setTimeout(warm, 300)
        cancelWarmup = () => window.clearTimeout(id)
      }
    }
    const stopFrame = () => {
      if (callback && callbackOwner) callbackOwner.cancelVideoFrameCallback(callback)
      if (raf) cancelAnimationFrame(raf)
      callback = 0; raf = 0; callbackOwner = null
      cancelWarmup?.(); cancelWarmup = null
      lastDraw = null; lastFrame = -Infinity
    }
    const fail = () => {
      if (disposed) return
      broken = true; stopFrame(); Object.values(videos).forEach(video => video.pause())
      setFailed(true); setReady(false)
    }
    const compile = (type: number, source: string) => {
      const shader = gl.createShader(type)!
      shaders.push(shader); gl.shaderSource(shader, source); gl.compileShader(shader)
      if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) throw new Error('Mascot shader unavailable')
      gl.attachShader(program, shader)
    }
    const draw = () => {
      const video = videos[playback.clip]
      if (disposed || broken || !live.current.active || video.readyState < 2) return
      try {
        const side = Math.max(1, Math.round(canvas.clientWidth * Math.min(devicePixelRatio || 1, compact?.matches ? 1 : 1.5)))
        if (canvas.width !== side || canvas.height !== side) { canvas.width = side; canvas.height = side }
        gl.viewport(0, 0, side, side)
        gl.bindTexture(gl.TEXTURE_2D, texture)
        gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, video)
        const now = performance.now()
        const delta = canPlay() && lastDraw !== null ? Math.min((now - lastDraw) / 1_000, 0.1) : 0
        lastDraw = now
        elapsed += delta
        const blend = 1 - Math.exp(-delta * 8)
        focus += ((live.current.activity === 'focused' ? 1 : 0) - focus) * blend
        reaction += ((playback.clip === 'react' ? 1 : 0) - reaction) * blend
        gl.uniform2f(maskStepUniform, 3 / Math.max(1, video.videoWidth / 2), 3 / Math.max(1, video.videoHeight))
        gl.uniform1f(elapsedUniform, elapsed)
        gl.uniform1f(focusUniform, focus)
        gl.uniform1f(reactionUniform, reaction)
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4)
        canvas.dataset.clip = playback.clip
        canvas.dataset.time = video.currentTime.toFixed(3)
        canvas.dataset.pending = String(playback.pending)
        if (!announced) { announced = true; setReady(true) }
        if (playback.clip === 'idle') warmReaction()
      } catch { fail() }
    }
    const frame = () => { callback = 0; raf = 0; if (canPlay()) { draw(); schedule() } }
    const fallbackFrame = (now: number) => {
      raf = 0
      if (!canPlay()) return
      // Old browsers must not upload the same video at the display's 60/120/144 Hz cadence.
      if (now - lastFrame >= 1_000 / 30) { lastFrame = now; draw() }
      schedule()
    }
    function schedule() {
      if (disposed || broken || callback || raf || !canPlay()) return
      const video = videos[playback.clip]
      if (typeof video.requestVideoFrameCallback === 'function') {
        callbackOwner = video; callback = video.requestVideoFrameCallback(frame)
      } else raf = requestAnimationFrame(fallbackFrame)
    }
    const start = () => {
      if (disposed || broken || !canPlay()) return
      const video = videos[playback.clip]
      loadClip(playback.clip)
      const version = ++playVersion
      video.playbackRate = live.current.playbackRate
      void video.play().then(schedule).catch(() => {
        // Cancellation by pause/unmount is expected; a genuine autoplay failure degrades.
        if (version !== playVersion || video !== videos[playback.clip]) return
        if (!disposed && canPlay() && video.paused) fail()
      })
    }
    const switchTo = (clip: MascotClip) => {
      stopFrame(); Object.values(videos).forEach(video => video.pause())
      videos[clip].currentTime = 0
      // Keep the previous final frame until the next clip has decoded its first frame.
      start()
    }
    const requestReaction = () => {
      if (!canPlay()) return
      loadClip('react')
      playback.requestReaction()
      const clip = playback.consumeAtRest(videos[playback.clip].currentTime)
      if (clip) switchTo(clip)
    }
    const sync = () => {
      if (disposed || broken) return
      if (!canPlay()) { stopFrame(); Object.values(videos).forEach(video => video.pause()); return }
      playback.setActivity(live.current.activity)
      if (playback.pending) loadClip('react')
      const clip = playback.consumeAtRest(videos[playback.clip].currentTime)
      if (clip) switchTo(clip)
      else start()
    }
    const lost = (event: Event) => { event.preventDefault(); fail() }
    const resize = new ResizeObserver(draw)
    try {
      compile(gl.VERTEX_SHADER, 'attribute vec2 p; varying vec2 uv; void main(){uv=(p+1.0)*0.5;gl_Position=vec4(p,0.0,1.0);}')
      compile(gl.FRAGMENT_SHADER, FRAGMENT_SHADER)
      gl.linkProgram(program)
      if (!gl.getProgramParameter(program, gl.LINK_STATUS)) throw new Error('Mascot program unavailable')
      maskStepUniform = gl.getUniformLocation(program, 'maskStep')
      elapsedUniform = gl.getUniformLocation(program, 'elapsed')
      focusUniform = gl.getUniformLocation(program, 'focus')
      reactionUniform = gl.getUniformLocation(program, 'reaction')
      gl.useProgram(program); gl.bindBuffer(gl.ARRAY_BUFFER, buffer)
      gl.activeTexture(gl.TEXTURE0); gl.uniform1i(gl.getUniformLocation(program, 'film'), 0)
      gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1,-1,1,-1,-1,1,1,1]), gl.STATIC_DRAW)
      const location = gl.getAttribLocation(program, 'p')
      gl.enableVertexAttribArray(location); gl.vertexAttribPointer(location, 2, gl.FLOAT, false, 0, 0)
      gl.bindTexture(gl.TEXTURE_2D, texture)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE)
      gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE)
      gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true)
      for (const clip of ['idle', 'react'] as const) {
        const video = document.createElement('video')
        video.muted = true; video.playsInline = true; video.preload = 'none'
        video.onloadeddata = () => { if (clip === playback.clip && canPlay()) { draw(); start() } }
        video.onended = () => { if (clip === playback.clip && canPlay()) switchTo(playback.end()) }
        video.onerror = fail
        videos[clip] = video
      }
      controller.current = { sync, react: requestReaction }
      resize.observe(canvas); canvas.addEventListener('webglcontextlost', lost)
      sync()
    } catch { fail() }
    return () => {
      disposed = true; controller.current = null; stopFrame(); resize.disconnect()
      canvas.removeEventListener('webglcontextlost', lost)
      Object.values(videos).forEach(video => {
        video.onended = null; video.onloadeddata = null; video.onerror = null
        video.pause(); video.removeAttribute('src'); video.load()
      })
      gl.deleteTexture(texture); gl.deleteBuffer(buffer); gl.deleteProgram(program)
      shaders.forEach(shader => gl.deleteShader(shader))
    }
  }, [motionMode])

  return <div className={styles.videoVisual} data-renderer={failed ? 'static-fallback' : 'video'}>
    <img className={styles.videoPoster} hidden={ready} src="/mascot/fox-poster.png" alt="" draggable={false} />
    <canvas ref={canvasRef} className={styles.videoCanvas} style={{ visibility: ready ? 'visible' : 'hidden' }} aria-hidden="true" />
  </div>
}
