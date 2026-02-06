/**
 * Browser audio processor for real-time voice communication.
 *
 * Handles:
 * - Microphone capture with echo cancellation
 * - Opus encoding of microphone input
 * - Opus decoding of incoming audio
 * - Jitter-buffered playback via AudioWorklet
 * - Audio visualization (input/output analyzers)
 *
 * Dependencies:
 * - audio-output-worklet.js (AudioWorklet for playback)
 * - Opus encoder/decoder workers (encoderWorker.min.js, decoderWorker.min.js)
 *
 * Usage:
 *   const processor = new AudioProcessor({
 *     onEncodedAudio: (opusData) => websocket.send(opusData),
 *     onMetrics: (metrics) => console.log('Buffer:', metrics.bufferMs),
 *     basePath: '/static/js'  // Path to worker files
 *   });
 *
 *   await processor.start();
 *   processor.playOpusData(incomingOpusData);
 *   processor.stop();
 */

class AudioProcessor {
  /**
   * @param {Object} options
   * @param {Function} options.onEncodedAudio - Callback when Opus data is available: (Uint8Array) => void
   * @param {Function} [options.onMetrics] - Callback for playback metrics: (metrics) => void
   * @param {Function} [options.onTurnChange] - Callback when playhead crosses turn boundary: ({oldTurnIdx, newTurnIdx}) => void
   * @param {string} [options.basePath='/js'] - Path prefix for worker files
   * @param {number} [options.sampleRate=24000] - Target sample rate for encoding
   * @param {boolean} [options.echoCancellation=true] - Enable browser echo cancellation
   */
  constructor(options) {
    this.onEncodedAudio = options.onEncodedAudio;
    this.onMetrics = options.onMetrics || (() => {});
    this.onTurnChange = options.onTurnChange || (() => {});
    this.basePath = options.basePath || '/js';
    this.sampleRate = options.sampleRate || 24000;
    this.echoCancellation = options.echoCancellation !== false; // default true

    this.audioContext = null;
    this.mediaStream = null;
    this.encoder = null;
    this.decoder = null;
    this.outputWorklet = null;
    this.inputAnalyser = null;
    this.outputAnalyser = null;

    this._started = false;
    this._decoderReady = false;
    this._decoderQueue = [];
  }

  /**
   * Request microphone access and start audio processing.
   * @returns {Promise<void>}
   */
  async start() {
    if (this._started) return;

    // Request microphone access
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: {
        channelCount: 1,
        echoCancellation: this.echoCancellation,
        autoGainControl: true,
        noiseSuppression: true,
      },
    });

    // Create audio context
    this.audioContext = new AudioContext();

    // Set up output worklet for playback
    await this.audioContext.audioWorklet.addModule(`${this.basePath}/audio-output-worklet.js`);
    this.outputWorklet = new AudioWorkletNode(this.audioContext, 'audio-output-processor');
    this.outputWorklet.connect(this.audioContext.destination);

    // Handle messages from worklet
    this.outputWorklet.port.onmessage = (event) => {
      if (event.data.type === 'metrics') {
        this.onMetrics(event.data);
      } else if (event.data.type === 'turn_change') {
        this.onTurnChange({
          oldTurnIdx: event.data.oldTurnIdx,
          newTurnIdx: event.data.newTurnIdx,
        });
      }
    };

    // Set up input chain
    const source = this.audioContext.createMediaStreamSource(this.mediaStream);

    // Input analyzer for visualization
    this.inputAnalyser = this.audioContext.createAnalyser();
    this.inputAnalyser.fftSize = 2048;
    source.connect(this.inputAnalyser);

    // Output analyzer for visualization
    this.outputAnalyser = this.audioContext.createAnalyser();
    this.outputAnalyser.fftSize = 2048;
    this.outputWorklet.connect(this.outputAnalyser);

    // Use preloaded decoder if available, otherwise create new one
    if (AudioProcessor._preloadWorker) {
      console.debug('AudioProcessor: using preloaded decoder');
      this.decoder = AudioProcessor._preloadWorker;
      AudioProcessor._preloadWorker = null; // Take ownership
      this._decoderReady = true; // Preloaded = already initialized
    } else {
      console.debug('AudioProcessor: creating new decoder worker');
      this.decoder = new Worker(`${this.basePath}/decoderWorker.min.js`);
      this._decoderReady = false;

      this.decoder.postMessage({
        command: 'init',
        bufferLength: (960 * this.audioContext.sampleRate) / this.sampleRate,
        decoderSampleRate: this.sampleRate,
        outputBufferSampleRate: this.audioContext.sampleRate,
        resampleQuality: 0,
      });

      // Give decoder time to init before allowing decode commands
      setTimeout(() => {
        if (!this._decoderReady) {
          console.debug('AudioProcessor: decoder init timeout, marking ready');
          this._decoderReady = true;
          this._flushDecoderQueue();
        }
      }, 500);
    }

    this.decoder.onmessage = (event) => {
      // First response means decoder is ready
      if (!this._decoderReady) {
        this._decoderReady = true;
        this._flushDecoderQueue();
      }

      if (!event.data) {
        console.debug('AudioProcessor: decoder returned empty data');
        return;
      }
      const frame = event.data[0];
      if (frame) {
        console.debug('AudioProcessor: decoded frame with', frame.length, 'samples');
        this.outputWorklet.port.postMessage({
          type: 'audio',
          frame: frame,
          stopS: this._pendingStopS,
          turnIdx: this._pendingTurnIdx,
          interrupted: this._pendingInterrupted,
        });
      }
    };
    this.decoder.onerror = (error) => {
      console.error('AudioProcessor: decoder worker error:', error);
    };

    // Set up Opus encoder using our standalone OpusEncoder
    if (typeof OpusEncoder === 'undefined') {
      throw new Error('OpusEncoder not loaded. Include opus-encoder.js via <script> tag.');
    }

    this.encoder = new OpusEncoder({
      encoderWorkerPath: `${this.basePath}/encoderWorker.min.js`,
      sampleRate: this.sampleRate,
      frameSize: 20, // 20ms frames
      maxFramesPerPage: 2, // 40ms chunks
      onData: (data) => {
        this.onEncodedAudio(data);
      },
    });

    // Resume context if suspended (browser autoplay policy)
    await this.audioContext.resume();

    // Start recording (share the audioContext)
    await this.encoder.start(this.mediaStream, this.audioContext);

    this._started = true;
  }

  /**
   * Play incoming Opus-encoded audio data.
   * @param {Uint8Array} opusData - Opus-encoded audio
   * @param {number} [stopS] - The stop_s timestamp for this audio (for text sync)
   * @param {number} [turnIdx] - The turn index for this audio (for turn boundary detection)
   * @param {boolean} [interrupted] - If true, this is the last audio before an interruption
   */
  playOpusData(opusData, stopS, turnIdx, interrupted) {
    if (!this.decoder) {
      console.warn('AudioProcessor: decoder not initialized');
      return;
    }
    console.debug('AudioProcessor: playing', opusData.length, 'bytes of Opus data, turnIdx:', turnIdx);

    const packet = { data: opusData, stopS, turnIdx, interrupted };

    if (!this._decoderReady) {
      // Queue until decoder sends first response
      this._decoderQueue.push(packet);
      return;
    }

    this._sendPacket(packet);
  }

  /** @private */
  _sendPacket(packet) {
    this._pendingStopS = packet.stopS;
    this._pendingTurnIdx = packet.turnIdx;
    this._pendingInterrupted = packet.interrupted || false;
    const copy = new Uint8Array(packet.data);
    this.decoder.postMessage(
      { command: 'decode', pages: copy },
      [copy.buffer]
    );
  }

  /** @private Flush queued packets after decoder is ready */
  _flushDecoderQueue() {
    console.debug('AudioProcessor: decoder ready, flushing', this._decoderQueue.length, 'packets');
    for (const packet of this._decoderQueue) {
      this._sendPacket(packet);
    }
    this._decoderQueue = [];
  }

  /**
   * Reset the playback buffer (e.g., on interruption).
   */
  resetPlayback() {
    if (this.outputWorklet) {
      this.outputWorklet.port.postMessage({ type: 'reset' });
    }
  }

  /**
   * Get input audio levels for visualization.
   * @returns {Uint8Array} Frequency data (0-255)
   */
  getInputLevels() {
    if (!this.inputAnalyser) return new Uint8Array(0);
    const data = new Uint8Array(this.inputAnalyser.frequencyBinCount);
    this.inputAnalyser.getByteFrequencyData(data);
    return data;
  }

  /**
   * Get output audio levels for visualization.
   * @returns {Uint8Array} Frequency data (0-255)
   */
  getOutputLevels() {
    if (!this.outputAnalyser) return new Uint8Array(0);
    const data = new Uint8Array(this.outputAnalyser.frequencyBinCount);
    this.outputAnalyser.getByteFrequencyData(data);
    return data;
  }

  /**
   * Stop audio processing and release resources.
   */
  stop() {
    if (!this._started) return;

    if (this.encoder) {
      this.encoder.stop();
      this.encoder = null;
    }

    if (this.decoder) {
      this.decoder.terminate();
      this.decoder = null;
    }

    if (this.outputWorklet) {
      this.outputWorklet.disconnect();
      this.outputWorklet = null;
    }

    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((track) => track.stop());
      this.mediaStream = null;
    }

    if (this.audioContext) {
      this.audioContext.close();
      this.audioContext = null;
    }

    this.inputAnalyser = null;
    this.outputAnalyser = null;
    this._started = false;
  }

  /**
   * Check if audio processing is active.
   * @returns {boolean}
   */
  get isRunning() {
    return this._started;
  }
}

/**
 * Preload the decoder WASM module in the background.
 * Call this at page load to warm up the decoder before it's needed.
 * @param {string} [basePath='/js'] - Path to worker files
 */
/**
 * Preload the decoder WASM module in the background.
 * Call this at page load to warm up the WASM cache before it's needed.
 * The actual AudioProcessor uses a queue to handle audio before decoder is ready.
 * @param {string} [basePath='/js'] - Path to worker files
 */
AudioProcessor.preloadDecoder = function(basePath = '/js') {
  if (AudioProcessor._preloadWorker) return;

  console.debug('AudioProcessor: preloading decoder WASM');

  // Just create worker and send init to trigger WASM compilation
  // The WASM will be cached by the browser for subsequent workers
  const worker = new Worker(`${basePath}/decoderWorker.min.js`);
  worker.postMessage({
    command: 'init',
    bufferLength: 4096,
    decoderSampleRate: 24000,
    outputBufferSampleRate: 48000,
    resampleQuality: 0,
  });
  AudioProcessor._preloadWorker = worker;
};

// Export for ES modules, or attach to window for script tags
if (typeof module !== 'undefined' && module.exports) {
  module.exports = AudioProcessor;
} else if (typeof window !== 'undefined') {
  window.AudioProcessor = AudioProcessor;
}
