'use client'

import { Canvas, useThree } from '@react-three/fiber'
import { Component, useEffect, type ReactNode } from 'react'

class SceneBoundary extends Component<{ children: ReactNode; fallback: ReactNode; onFailure: () => void }, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch() { this.props.onFailure() }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}

function SceneLifecycle({ active, animate, onFailure }: { active: boolean; animate: boolean; onFailure: () => void }) {
  const { gl, invalidate } = useThree()
  useEffect(() => {
    const lost = (event: Event) => { event.preventDefault(); onFailure() }
    gl.domElement.addEventListener('webglcontextlost', lost)
    return () => gl.domElement.removeEventListener('webglcontextlost', lost)
  }, [gl, onFailure])
  useEffect(() => {
    if (!active) return
    invalidate()
    if (!animate) return
    const timer = window.setInterval(() => invalidate(), 1000 / 30)
    return () => window.clearInterval(timer)
  }, [active, animate, invalidate])
  return null
}

export function SceneCanvas({ children, active, animate = false, onFailure, fallback, camera = [0, 0, 4] }: {
  children: ReactNode; active: boolean; animate?: boolean; onFailure: () => void; fallback: ReactNode
  camera?: [number, number, number]
}) {
  const dpr = window.matchMedia('(max-width: 640px)').matches ? 1 : Math.min(window.devicePixelRatio || 1, 1.5)
  return <SceneBoundary fallback={fallback} onFailure={onFailure}>
    <Canvas aria-hidden="true" dpr={dpr} frameloop={active ? 'demand' : 'never'}
      camera={{ position: camera, fov: 38 }}
      gl={{ alpha: true, antialias: true, powerPreference: 'low-power' }} fallback={fallback}>
      <SceneLifecycle active={active} animate={animate} onFailure={onFailure} />
      {children}
    </Canvas>
  </SceneBoundary>
}
