class LumenPcmCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.targetRate = options.processorOptions?.targetRate ?? 16000
    this.ratio = sampleRate / this.targetRate
    this.phase = 0
    this.pending = []
    this.frameSamples = Math.max(1, Math.round(this.targetRate * 0.02))
  }

  process(inputs) {
    const input = inputs[0]?.[0]
    if (!input) return true
    while (this.phase < input.length) {
      const index = Math.floor(this.phase)
      const next = Math.min(index + 1, input.length - 1)
      const fraction = this.phase - index
      const sample = input[index] + (input[next] - input[index]) * fraction
      this.pending.push(Math.max(-1, Math.min(1, sample)))
      this.phase += this.ratio
    }
    this.phase -= input.length
    while (this.pending.length >= this.frameSamples) {
      const frame = this.pending.splice(0, this.frameSamples)
      const pcm = new Int16Array(frame.length)
      for (let index = 0; index < frame.length; index += 1) {
        const value = frame[index]
        pcm[index] = value < 0 ? value * 0x8000 : value * 0x7fff
      }
      this.port.postMessage(pcm.buffer, [pcm.buffer])
    }
    return true
  }
}

class LumenPcmPlaybackProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super()
    this.sourceRate = options.processorOptions?.sourceRate ?? 24000
    this.ratio = this.sourceRate / sampleRate
    this.buffers = []
    this.buffer = null
    this.position = 0
    this.port.onmessage = (event) => {
      if (event.data?.type === 'clear') {
        this.buffers = []; this.buffer = null; this.position = 0
        return
      }
      if (!(event.data instanceof ArrayBuffer)) return
      const pcm = new Int16Array(event.data)
      const samples = new Float32Array(pcm.length)
      for (let index = 0; index < pcm.length; index += 1) samples[index] = pcm[index] / 0x8000
      this.buffers.push(samples)
    }
  }

  nextSample() {
    if (!this.buffer || this.position >= this.buffer.length - 1) {
      this.buffer = this.buffers.shift() ?? null
      this.position = 0
      if (!this.buffer) return 0
    }
    const index = Math.floor(this.position)
    const next = Math.min(index + 1, this.buffer.length - 1)
    const fraction = this.position - index
    const value = this.buffer[index] + (this.buffer[next] - this.buffer[index]) * fraction
    this.position += this.ratio
    return value
  }

  process(_inputs, outputs) {
    const output = outputs[0]?.[0]
    if (!output) return true
    for (let index = 0; index < output.length; index += 1) output[index] = this.nextSample()
    return true
  }
}

registerProcessor('lumen-pcm-capture', LumenPcmCaptureProcessor)
registerProcessor('lumen-pcm-playback', LumenPcmPlaybackProcessor)
