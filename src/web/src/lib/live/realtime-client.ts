import { lumenApi, liveMediaWebSocketUrl, subscribeLive } from '@/lib/api/client'
import type { LiveEventEnvelope, LiveStartResponse } from '@/lib/api/types'

export interface RealtimeVoiceCallbacks {
  onEvent: (event: LiveEventEnvelope) => void
  onLocalEvent?: (event: Record<string, unknown>) => void
  onPlaybackEnded?: () => void
  onConnectionLost: (message: string) => void
}

async function waitForIceGathering(peer: RTCPeerConnection) {
  if (peer.iceGatheringState === 'complete') return
  await new Promise<void>((resolve) => {
    const timeout = window.setTimeout(resolve, 2_000)
    const listener = () => {
      if (peer.iceGatheringState !== 'complete') return
      window.clearTimeout(timeout)
      peer.removeEventListener('icegatheringstatechange', listener)
      resolve()
    }
    peer.addEventListener('icegatheringstatechange', listener)
  })
}

export class RealtimeVoiceClient {
  private peer: RTCPeerConnection | null = null
  private channel: RTCDataChannel | null = null
  private stream: MediaStream | null = null
  private audio: HTMLAudioElement | null = null
  private mediaSocket: WebSocket | null = null
  private audioContext: AudioContext | null = null
  private captureSource: MediaStreamAudioSourceNode | null = null
  private captureNode: AudioWorkletNode | null = null
  private playbackNode: AudioWorkletNode | null = null
  private silentGain: GainNode | null = null
  private closeEvents: (() => void) | null = null
  private callbacks: RealtimeVoiceCallbacks | null = null
  private sessionId: string | null = null
  private liveSessionId: string | null = null
  private stopped = false

  async connect(
    sessionId: string,
    callbacks: RealtimeVoiceCallbacks,
    deviceId?: string,
  ): Promise<string> {
    this.callbacks = callbacks
    this.sessionId = sessionId
    this.stopped = false
    this.stream = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: deviceId ? { exact: deviceId } : undefined,
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })

    const peer = this.prepareWebRtc(callbacks)
    const offer = await peer.createOffer()
    await peer.setLocalDescription(offer)
    await waitForIceGathering(peer)
    const sdp = peer.localDescription?.sdp
    if (!sdp) throw new Error('浏览器未生成有效的 WebRTC SDP')

    const started = await lumenApi.startLive(sessionId, sdp, crypto.randomUUID())
    this.liveSessionId = started.liveSessionId
    this.closeEvents = subscribeLive(
      started.liveSessionId,
      (event) => {
        this.handleAuthoritativeEvent(event)
        callbacks.onEvent(event)
      },
      callbacks.onConnectionLost,
    )

    if (started.media.kind === 'direct_webrtc') {
      const answer = started.media.answer_sdp ?? started.answerSdp
      if (!answer) throw new Error('Realtime Provider 未返回 WebRTC SDP')
      await peer.setRemoteDescription({ type: 'answer', sdp: answer })
    } else if (started.media.kind === 'host_websocket') {
      this.closeDirectWebRtc(false)
      await this.connectHostMedia(started)
    } else {
      throw new Error(`当前 Web 客户端不支持媒体模式：${started.media.kind}`)
    }
    return started.liveSessionId
  }

  async interrupt() {
    if (!this.liveSessionId) return
    window.speechSynthesis?.cancel()
    if (this.channel?.readyState === 'open') {
      this.channel.send(JSON.stringify({ type: 'response.cancel' }))
    }
    await lumenApi.interruptLive(this.liveSessionId)
  }

  setMuted(muted: boolean) {
    for (const track of this.stream?.getAudioTracks() ?? []) track.enabled = !muted
  }

  async selectDevice(deviceId: string) {
    if (!this.stream) return
    const replacement = await navigator.mediaDevices.getUserMedia({
      audio: {
        deviceId: { exact: deviceId },
        echoCancellation: true,
        noiseSuppression: true,
        autoGainControl: true,
      },
    })
    const nextTrack = replacement.getAudioTracks()[0]
    if (!nextTrack) {
      replacement.getTracks().forEach((track) => track.stop())
      throw new Error('无法切换麦克风设备')
    }
    const sender = this.peer?.getSenders().find((item) => item.track?.kind === 'audio')
    if (sender) await sender.replaceTrack(nextTrack)
    this.stream.getTracks().forEach((track) => track.stop())
    this.stream = replacement
    if (this.audioContext && this.captureNode) this.connectCaptureSource()
  }

  async devices(): Promise<MediaDeviceInfo[]> {
    return (await navigator.mediaDevices.enumerateDevices()).filter(
      (device) => device.kind === 'audioinput',
    )
  }

  async stop() {
    this.stopped = true
    window.speechSynthesis?.cancel()
    this.closeEvents?.()
    this.closeEvents = null
    this.closeDirectWebRtc(true)
    this.mediaSocket?.close()
    this.mediaSocket = null
    this.captureSource?.disconnect()
    this.captureSource = null
    this.captureNode?.disconnect()
    this.captureNode = null
    this.playbackNode?.disconnect()
    this.playbackNode = null
    this.silentGain?.disconnect()
    this.silentGain = null
    await this.audioContext?.close().catch(() => undefined)
    this.audioContext = null
    this.stream?.getTracks().forEach((track) => track.stop())
    this.stream = null
    const liveSessionId = this.liveSessionId
    this.liveSessionId = null
    if (liveSessionId) await lumenApi.endLive(liveSessionId).catch(() => undefined)
  }

  private prepareWebRtc(callbacks: RealtimeVoiceCallbacks): RTCPeerConnection {
    const peer = new RTCPeerConnection()
    this.peer = peer
    for (const track of this.stream?.getAudioTracks() ?? []) peer.addTrack(track, this.stream!)

    const audio = new Audio()
    audio.autoplay = true
    audio.setAttribute('playsinline', '')
    this.audio = audio
    peer.ontrack = (event) => {
      audio.srcObject = event.streams[0]
      void audio.play().catch(() => callbacks.onConnectionLost('浏览器阻止了语音自动播放'))
    }

    const channel = peer.createDataChannel('oai-events')
    this.channel = channel
    channel.onmessage = (message) => {
      try {
        callbacks.onLocalEvent?.(JSON.parse(String(message.data)) as Record<string, unknown>)
      } catch {
        // Diagnostics are best effort; Host SSE remains authoritative.
      }
    }
    peer.onconnectionstatechange = () => {
      if (this.stopped) return
      if (peer.connectionState === 'failed') callbacks.onConnectionLost('WebRTC 连接失败')
      if (peer.connectionState === 'disconnected') {
        window.setTimeout(() => {
          if (!this.stopped && peer.connectionState === 'disconnected') {
            callbacks.onConnectionLost('WebRTC 连接已断开')
          }
        }, 1_500)
      }
    }
    return peer
  }

  private async connectHostMedia(started: LiveStartResponse) {
    const mediaPath = started.media.media_path
    if (!mediaPath) throw new Error('Host PCM 媒体握手缺少 WebSocket 路径')
    const context = new AudioContext({ latencyHint: 'interactive' })
    this.audioContext = context
    await context.audioWorklet.addModule('/lumen-pcm-worklet.js')
    await context.resume()

    const capture = new AudioWorkletNode(context, 'lumen-pcm-capture', {
      processorOptions: { targetRate: started.media.input_sample_rate ?? 16_000 },
    })
    const playback = new AudioWorkletNode(context, 'lumen-pcm-playback', {
      processorOptions: { sourceRate: started.media.output_sample_rate ?? 24_000 },
    })
    const silent = context.createGain()
    silent.gain.value = 0
    capture.connect(silent).connect(context.destination)
    playback.connect(context.destination)
    this.captureNode = capture
    this.playbackNode = playback
    this.silentGain = silent
    this.connectCaptureSource()

    const socket = new WebSocket(liveMediaWebSocketUrl(mediaPath))
    socket.binaryType = 'arraybuffer'
    this.mediaSocket = socket
    await new Promise<void>((resolve, reject) => {
      socket.onopen = () => resolve()
      socket.onerror = () => reject(new Error('无法连接 Lumen PCM 媒体通道'))
    })
    capture.port.onmessage = (event: MessageEvent<ArrayBuffer>) => {
      if (socket.readyState === WebSocket.OPEN) socket.send(event.data)
    }
    socket.onmessage = (event) => {
      if (event.data instanceof ArrayBuffer) playback.port.postMessage(event.data, [event.data])
    }
    socket.onclose = () => {
      if (!this.stopped) this.callbacks?.onConnectionLost('PCM 媒体连接已断开')
    }
  }

  private connectCaptureSource() {
    if (!this.audioContext || !this.captureNode || !this.stream) return
    this.captureSource?.disconnect()
    this.captureSource = this.audioContext.createMediaStreamSource(this.stream)
    this.captureSource.connect(this.captureNode)
  }

  private handleAuthoritativeEvent(event: LiveEventEnvelope) {
    if (event.type !== 'live.response.approved') return
    const answer = event.data.answer
    if (typeof answer !== 'string' || !answer.trim() || !window.speechSynthesis) return
    window.speechSynthesis.cancel()
    const utterance = new SpeechSynthesisUtterance(answer)
    utterance.lang = /[\u3400-\u9fff]/u.test(answer) ? 'zh-CN' : navigator.language
    utterance.onend = () => this.callbacks?.onPlaybackEnded?.()
    utterance.onerror = () => this.callbacks?.onPlaybackEnded?.()
    window.speechSynthesis.speak(utterance)
  }

  private closeDirectWebRtc(clearStream: boolean) {
    this.channel?.close()
    this.channel = null
    this.peer?.close()
    this.peer = null
    if (this.audio) this.audio.srcObject = null
    this.audio = null
    if (clearStream) {
      this.stream?.getTracks().forEach((track) => track.stop())
      this.stream = null
    }
  }
}
