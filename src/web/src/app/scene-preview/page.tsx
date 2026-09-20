'use client'

import dynamic from 'next/dynamic'
import { useRef, useState } from 'react'
import { VoiceFocus } from '@/components/voice-focus'
import type { LiveUiStatus } from '@/lib/live/live-reducer'
import { silentAudioLevels } from '@/lib/live/realtime-client'

const ModelPreview = dynamic(() => import('@/components/model-preview').then(module => module.ModelPreview), { ssr: false })

/** Small self-contained cube for a reproducible local GLB check; not a mascot asset. */
function cubeGlb() {
  const positions = new Float32Array([-1,-1,-1, 1,-1,-1, 1,1,-1, -1,1,-1, -1,-1,1, 1,-1,1, 1,1,1, -1,1,1])
  const indices = new Uint16Array([0,2,1, 0,3,2, 4,5,6, 4,6,7, 0,1,5, 0,5,4, 3,7,6, 3,6,2, 0,4,7, 0,7,3, 1,2,6, 1,6,5])
  const binary = new Uint8Array(positions.byteLength + indices.byteLength)
  binary.set(new Uint8Array(positions.buffer)); binary.set(new Uint8Array(indices.buffer), positions.byteLength)
  const json = new TextEncoder().encode(JSON.stringify({
    asset: { version: '2.0' }, scene: 0, scenes: [{ nodes: [0] }], nodes: [{ mesh: 0 }],
    buffers: [{ byteLength: binary.length }],
    bufferViews: [{ buffer: 0, byteLength: positions.byteLength }, { buffer: 0, byteOffset: positions.byteLength, byteLength: indices.byteLength }],
    accessors: [{ bufferView: 0, componentType: 5126, count: 8, type: 'VEC3', min: [-1,-1,-1], max: [1,1,1] }, { bufferView: 1, componentType: 5123, count: 36, type: 'SCALAR' }],
    materials: [{ pbrMetallicRoughness: { baseColorFactor: [.72,.5,.3,1], metallicFactor: .1, roughnessFactor: .6 } }],
    meshes: [{ primitives: [{ attributes: { POSITION: 0 }, indices: 1, material: 0 }] }],
  }))
  const padded = Math.ceil(json.length / 4) * 4, result = new ArrayBuffer(28 + padded + binary.length), view = new DataView(result)
  view.setUint32(0, 0x46546c67, true); view.setUint32(4, 2, true); view.setUint32(8, result.byteLength, true)
  view.setUint32(12, padded, true); view.setUint32(16, 0x4e4f534a, true)
  const bytes = new Uint8Array(result); bytes.fill(32, 20, 20 + padded); bytes.set(json, 20)
  view.setUint32(20 + padded, binary.length, true); view.setUint32(24 + padded, 0x004e4942, true); bytes.set(binary, 28 + padded)
  return result
}

export default function ScenePreview() {
  const [focus, setFocus] = useState(false), [status, setStatus] = useState<LiveUiStatus>('listening')
  const [muted, setMuted] = useState(false), [buffer, setBuffer] = useState<ArrayBuffer | null>(null)
  const [error, setError] = useState('')
  const levels = useRef({ ...silentAudioLevels })
  return <main className="scene-preview-page">
    <header><a href="/">LUMEN</a><span>3D 场景 · 本地验收</span></header>
    <h1>语音形体与模型预览</h1>
    <p>这里使用模拟状态和音量，不申请麦克风，也不建立 Live 通话。</p>
    <section><h2>语音专注视图</h2><button type="button" onClick={() => setFocus(true)}>打开语音视图</button></section>
    <section><h2>GLB 预览</h2><div className="model-preview-toolbar">
      <button type="button" onClick={() => setBuffer(cubeGlb())}>加载示例立方体</button>
      <label>加载本地 GLB<input type="file" accept=".glb" onChange={async event => {
        const file = event.target.files?.[0]; setError('')
        if (!file) return
        if (file.size > 20 * 1024 * 1024) { setError('文件超过 20 MiB'); return }
        setBuffer(await file.arrayBuffer())
      }} /></label>
      {buffer && <button type="button" onClick={() => setBuffer(null)}>关闭模型</button>}
    </div>{error && <p role="alert">{error}</p>}{buffer && <ModelPreview buffer={buffer} />}</section>
    {focus && <VoiceFocus levels={levels} status={status} muted={muted} onClose={() => setFocus(false)} onVisibility={() => undefined}>
      <div className="scene-preview-voice-controls">
        <label>模拟状态<select value={status} onChange={event => setStatus(event.target.value as LiveUiStatus)}>
          {([['listening','聆听'],['processing','思考'],['tool_running','工具执行'],['assistant_speaking','助手说话'],['approval_pending','等待审批'],['reconnecting','重连'],['error','错误']] as const)
            .map(([value, label]) => <option key={value} value={value}>{label}</option>)}
        </select></label>
        <label>模拟 RMS<input type="range" min="0" max="1" step=".01" defaultValue="0" onChange={event => {
          const value = Number(event.target.value); levels.current = { input: value, output: value, outputAvailable: true }
        }} /></label>
        <button type="button" onClick={() => setMuted(!muted)}>{muted ? '取消静音' : '静音'}</button>
        <button type="button" onClick={() => setFocus(false)}>结束演示</button>
        {status === 'approval_pending' && <div className="live-approval"><strong>演示审批：write_file</strong><p>不会执行任何文件操作。</p>
          <div><button type="button" onClick={() => setStatus('listening')}>拒绝</button><button type="button" onClick={() => setStatus('processing')}>允许一次</button></div></div>}
      </div>
    </VoiceFocus>}
  </main>
}
