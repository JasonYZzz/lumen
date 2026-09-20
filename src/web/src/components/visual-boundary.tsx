'use client'

import { Component, type ReactNode } from 'react'

/** Own the lazy visual at the DOM seam, before its renderer module can load. */
export class VisualBoundary extends Component<{
  children: ReactNode; fallback: ReactNode; onFailure?: () => void
}, { failed: boolean }> {
  state = { failed: false }
  static getDerivedStateFromError() { return { failed: true } }
  componentDidCatch() { this.props.onFailure?.() }
  render() { return this.state.failed ? this.props.fallback : this.props.children }
}
