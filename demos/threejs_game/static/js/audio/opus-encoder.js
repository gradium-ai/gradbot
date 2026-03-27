/**
 * Lightweight Opus encoder wrapper for browser audio.
 *
 * Uses the opus-recorder encoder worker to encode PCM audio to Opus.
 * This is a standalone implementation that doesn't require npm.
 *
 * Usage:
 *   const encoder = new OpusEncoder({
 *     encoderWorkerPath: '/js/encoderWorker.min.js',
 *     sampleRate: 24000,
 *     onData: (opusData) => websocket.send(opusData),
 *   });
 *   await encoder.start(mediaStream);
 *   encoder.stop();
 */

class OpusEncoder {
  /**
   * @param {Object} options
   * @param {string} options.encoderWorkerPath - Path to encoderWorker.min.js
   * @param {Function} options.onData - Callback when Opus data is available: (Uint8Array) => void
   * @param {number} [options.sampleRate=24000] - Target sample rate (24000 recommended for voice)
   * @param {number} [options.frameSize=20] - Frame size in ms (20ms recommended)
   * @param {number} [options.maxFramesPerPage=2] - Frames per Ogg page (2 = 40ms chunks)
   */
  constructor(options) {
    this.encoderWorkerPath = options.encoderWorkerPath;
    this.onData = options.onData;
    this.sampleRate = options.sampleRate || 24000;
    this.frameSize = options.frameSize || 20;
    this.maxFramesPerPage = options.maxFramesPerPage || 2;

    this.worker = null;
    this.audioContext = null;
    this.sourceNode = null;
    this.processorNode = null;
    this._started = false;
    this._encodedSamplePosition = 0;
  }

  /**
   * Start encoding audio from a MediaStream.
   * @param {MediaStream} mediaStream - Audio stream from getUserMedia
   * @param {AudioContext} [audioContext] - Optional existing AudioContext to reuse
   */
  async start(mediaStream, audioContext = null) {
    if (this._started) return;

    this.audioContext = audioContext || new AudioContext();

    // Create encoder worker
    this.worker = new Worker(this.encoderWorkerPath);
    this.worker.onmessage = (event) => {
      if (event.data.message === 'done') {
        // Encoding complete (for non-streaming mode)
        return;
      }
      if (event.data.message === 'page') {
        // Streaming mode: got an Ogg page
        this._encodedSamplePosition = event.data.encodedSamplePosition || 0;
        this.onData(new Uint8Array(event.data.page));
      }
    };

    // Calculate buffer sizes
    // Opus works internally at 48kHz, so we calculate buffer to align with frame boundaries
    const bufferLength = Math.round((960 * this.audioContext.sampleRate) / this.sampleRate);

    // Initialize encoder worker
    this.worker.postMessage({
      command: 'init',
      encoderSampleRate: this.sampleRate,
      bufferLength: bufferLength,
      numberOfChannels: 1,
      maxFramesPerPage: this.maxFramesPerPage,
      encoderApplication: 2049, // VOIP
      encoderFrameSize: this.frameSize,
      encoderComplexity: 3,
      originalSampleRate: this.audioContext.sampleRate,
      resampleQuality: 3,
      streamPages: true,
    });

    // Create source from media stream
    this.sourceNode = this.audioContext.createMediaStreamSource(mediaStream);

    // Use ScriptProcessor for capturing audio
    // (AudioWorklet would be cleaner but requires more setup)
    const bufferSize = 4096;
    this.processorNode = this.audioContext.createScriptProcessor(bufferSize, 1, 1);

    this.processorNode.onaudioprocess = (event) => {
      if (!this._started) return;

      const inputBuffer = event.inputBuffer.getChannelData(0);
      // Copy buffer since it will be reused
      const buffer = new Float32Array(inputBuffer.length);
      buffer.set(inputBuffer);

      this.worker.postMessage({
        command: 'encode',
        buffers: [buffer],
      });
    };

    // Connect the chain
    this.sourceNode.connect(this.processorNode);
    // Connect to destination to keep the processor running (output is silent)
    this.processorNode.connect(this.audioContext.destination);

    this._started = true;
  }

  /**
   * Get the current encoded sample position (for timing/sync).
   * @returns {number} Encoded sample position
   */
  get encodedSamplePosition() {
    return this._encodedSamplePosition;
  }

  /**
   * Stop encoding and release resources.
   */
  stop() {
    if (!this._started) return;

    this._started = false;

    if (this.processorNode) {
      this.processorNode.disconnect();
      this.processorNode = null;
    }

    if (this.sourceNode) {
      this.sourceNode.disconnect();
      this.sourceNode = null;
    }

    if (this.worker) {
      // Signal end of encoding
      this.worker.postMessage({ command: 'done' });
      this.worker.terminate();
      this.worker = null;
    }
  }
}

// Export for ES modules, or attach to window for script tags
if (typeof module !== 'undefined' && module.exports) {
  module.exports = OpusEncoder;
} else if (typeof window !== 'undefined') {
  window.OpusEncoder = OpusEncoder;
}
