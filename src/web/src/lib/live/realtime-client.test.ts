// @vitest-environment jsdom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RealtimeVoiceClient, audioRms } from './realtime-client'

const api = vi.hoisted(() => ({ startLive: vi.fn(), endLive: vi.fn(), interruptLive: vi.fn(), subscribe: vi.fn() }))
vi.mock('@/lib/api/client', () => ({ lumenApi: api, subscribeLive: api.subscribe, liveMediaWebSocketUrl: () => 'ws://localhost/media' }))
class FakeNode {
  links = new Set<FakeNode>(); gain = { value: 1 }; fftSize = 512
  port = { onmessage: null, postMessage: vi.fn() }
  connect(node: FakeNode) { this.links.add(node); return node }
  disconnect(node?: FakeNode) { if (node) this.links.delete(node); else this.links.clear() }
  getFloatTimeDomainData(samples: Float32Array) { samples.fill(.25) }
}
class FakeStream {
  track = { enabled: true, kind: 'audio', stop: vi.fn() }
  getTracks() { return [this.track] }
  getAudioTracks() { return [this.track] }
}
class FakeContext {
  static instances: FakeContext[] = []
  destination = new FakeNode(); nodes: FakeNode[] = []
  audioWorklet = { addModule: vi.fn().mockResolvedValue(undefined) }
  close = vi.fn().mockResolvedValue(undefined); resume = vi.fn().mockResolvedValue(undefined)
  constructor() { FakeContext.instances.push(this) }
  createGain() { const node = new FakeNode(); this.nodes.push(node); return node }
  createAnalyser() { return this.createGain() }
  createMediaStreamSource() { return this.createGain() }
}
class FakePeer {
  static instances: FakePeer[] = []
  iceGatheringState = 'complete'; localDescription = { sdp: 'offer' }; ontrack: ((event: { streams: FakeStream[] }) => void) | null = null
  createOffer = vi.fn().mockResolvedValue({ sdp: 'offer' }); setLocalDescription = vi.fn().mockResolvedValue(undefined)
  setRemoteDescription = vi.fn().mockResolvedValue(undefined); close = vi.fn(); sender = { track: { kind: 'audio' }, replaceTrack: vi.fn().mockResolvedValue(undefined) }
  constructor() { FakePeer.instances.push(this) }
  addTrack() {} getSenders() { return [this.sender] }
  createDataChannel() { return { close: vi.fn(), readyState: 'open', send: vi.fn() } }
}
class FakeSocket {
  static OPEN = 1; readyState = 1; onopen: (() => void) | null = null; onclose: (() => void) | null = null
  send = vi.fn(); close = vi.fn(() => this.onclose?.())
  constructor() { queueMicrotask(() => this.onopen?.()) }
}
const streams: FakeStream[] = [], worklets: FakeNode[] = []
let getUserMedia: ReturnType<typeof vi.fn>
beforeEach(() => {
  vi.useFakeTimers(); vi.resetAllMocks(); FakeContext.instances = []; FakePeer.instances = []; streams.length = 0; worklets.length = 0
  getUserMedia = vi.fn(async () => { const stream = new FakeStream(); streams.push(stream); return stream })
  Object.defineProperty(navigator, 'mediaDevices', { configurable: true, value: { getUserMedia, enumerateDevices: vi.fn().mockResolvedValue([]) } })
  vi.stubGlobal('AudioContext', FakeContext); vi.stubGlobal('RTCPeerConnection', FakePeer); vi.stubGlobal('MediaStream', FakeStream)
  vi.stubGlobal('Audio', class { srcObject: FakeStream | null = null; setAttribute() {}; play = vi.fn().mockResolvedValue(undefined) })
  vi.stubGlobal('WebSocket', FakeSocket)
  vi.stubGlobal('AudioWorkletNode', class extends FakeNode { constructor() { super(); worklets.push(this) } })
  api.startLive.mockResolvedValue({ liveSessionId: 'live-1', media: { kind: 'direct_webrtc', answer_sdp: 'answer' } })
  api.endLive.mockResolvedValue(undefined); api.interruptLive.mockResolvedValue(undefined); api.subscribe.mockReturnValue(vi.fn())
})
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals() })
const callbacks = () => ({ onEvent: vi.fn(), onConnectionLost: vi.fn(), onAudioLevels: vi.fn(), onPlaybackEnded: vi.fn() })

describe('local audio analysis lifecycle', () => {
  it('measures RMS, including silence and bounded loud input', () => {
    expect(audioRms(new Float32Array())).toBe(0); expect(audioRms(new Float32Array([0, 0]))).toBe(0)
    expect(audioRms(new Float32Array([.5, -.5]))).toBe(.5); expect(audioRms(new Float32Array([2]))).toBe(1)
  })
  it('does not create an analysis context until expanded, reuses streams, and silences muted samples', async () => {
    const client = new RealtimeVoiceClient(), events = callbacks()
    await client.connect('session', events)
    expect(FakeContext.instances).toHaveLength(0)
    FakePeer.instances[0].ontrack?.({ streams: [new FakeStream()] })
    client.setVisualizationActive(true)
    expect(getUserMedia).toHaveBeenCalledTimes(1); expect(FakeContext.instances).toHaveLength(1)
    expect(events.onAudioLevels).toHaveBeenLastCalledWith({ input: .25, output: .25, outputAvailable: true })
    client.setMuted(true); await vi.advanceTimersByTimeAsync(40)
    expect(events.onAudioLevels).toHaveBeenLastCalledWith({ input: 0, output: .25, outputAvailable: true })
    await client.selectDevice('next')
    expect(streams[0].track.stop).toHaveBeenCalled(); expect(streams[1].track.enabled).toBe(false)
    expect(FakeContext.instances).toHaveLength(1)
    client.setVisualizationActive(false)
    expect(FakeContext.instances[0].close).toHaveBeenCalledTimes(1)
    expect(streams[1].track.stop).not.toHaveBeenCalled()
    const calls = events.onAudioLevels.mock.calls.length
    await vi.advanceTimersByTimeAsync(200); expect(events.onAudioLevels).toHaveBeenCalledTimes(calls)
    await client.stop(); expect(streams[1].track.stop).toHaveBeenCalled(); expect(vi.getTimerCount()).toBe(0)
  })
  it('branches host PCM through silence without disconnecting audible playback', async () => {
    api.startLive.mockResolvedValue({ liveSessionId: 'live-host', media: { kind: 'host_websocket', media_path: '/media' } })
    const client = new RealtimeVoiceClient(), events = callbacks()
    await client.connect('session', events)
    const context = FakeContext.instances[0], playback = worklets[1]
    expect(FakeContext.instances).toHaveLength(1); expect(playback.links.has(context.destination)).toBe(true)
    client.setVisualizationActive(true)
    expect(FakeContext.instances).toHaveLength(1); expect(playback.links.size).toBe(2)
    client.setVisualizationActive(false)
    expect(playback.links).toEqual(new Set([context.destination])); expect(context.close).not.toHaveBeenCalled()
    await client.stop(); expect(playback.links.size).toBe(0); expect(context.close).toHaveBeenCalledTimes(1)
    expect(context.nodes.every(node => node.links.size === 0)).toBe(true); expect(vi.getTimerCount()).toBe(0)
  })
  it('stops a microphone obtained after cancellation without starting a Host session', async () => {
    let deliver!: (stream: FakeStream) => void
    getUserMedia.mockImplementation(() => new Promise(resolve => { deliver = resolve }))
    const client = new RealtimeVoiceClient(), pending = client.connect('session', callbacks())
    await client.stop(); const stream = new FakeStream(); deliver(stream)
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' })
    expect(stream.track.stop).toHaveBeenCalled(); expect(api.startLive).not.toHaveBeenCalled()
  })
  it('ends a Host session returned after cancellation', async () => {
    let deliver!: (response: unknown) => void
    api.startLive.mockImplementation(() => new Promise(resolve => { deliver = resolve }))
    const client = new RealtimeVoiceClient(), pending = client.connect('session', callbacks())
    await vi.waitFor(() => expect(api.startLive).toHaveBeenCalled())
    await client.stop(); deliver({ liveSessionId: 'late', media: { kind: 'direct_webrtc', answer_sdp: 'answer' } })
    await expect(pending).rejects.toMatchObject({ name: 'AbortError' }); expect(api.endLive).toHaveBeenCalledWith('late')
    expect(FakeContext.instances).toHaveLength(0)
  })
  it('reports TTS as unavailable output and ignores stale speech completion after interrupt/stop', async () => {
    const speech = { cancel: vi.fn(), speak: vi.fn() }
    vi.stubGlobal('speechSynthesis', speech); vi.stubGlobal('SpeechSynthesisUtterance', class { constructor(public text: string) {} })
    const client = new RealtimeVoiceClient(), events = callbacks()
    await client.connect('session', events); client.setVisualizationActive(true)
    const emit = api.subscribe.mock.calls[0][1]
    emit({ type: 'live.response.approved', data: { answer: '你好' } })
    await vi.advanceTimersByTimeAsync(40)
    expect(events.onAudioLevels).toHaveBeenLastCalledWith({ input: .25, output: 0, outputAvailable: false })
    const utterance = speech.speak.mock.calls[0][0]
    await client.interrupt(); utterance.onend()
    expect(events.onPlaybackEnded).not.toHaveBeenCalled(); await client.stop(); emit({ type: 'live.response.approved', data: { answer: '旧事件' } })
    expect(speech.speak).toHaveBeenCalledTimes(1)
  })
})
