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
    this._oggHeaderPages = []; // Cached Ogg header pages for decoder recreation
    this._oggHeadersCaptured = false;
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
    this.audioContext = new AudioContext({ sampleRate: 48000 });

    // Resume context before addModule — Safari hangs on addModule if context is suspended
    await this.audioContext.resume();

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
      this._setupDecoderHandlers();
    } else {
      this._createDecoder();
    }

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

    // Start recording (share the audioContext)
    await this.encoder.start(this.mediaStream, this.audioContext);

    this._started = true;
  }

  /**
   * Play incoming raw PCM audio data (16-bit signed LE, 48kHz mono).
   * Bypasses the Opus decoder entirely — converts Int16 to Float32 and sends to worklet.
   * @param {Uint8Array} pcmData - Raw PCM audio (Int16LE, 48kHz, mono)
   * @param {number} [stopS] - The stop_s timestamp for this audio (for text sync)
   * @param {number} [turnIdx] - The turn index for this audio (for turn boundary detection)
   * @param {boolean} [interrupted] - If true, this is the last audio before an interruption
   */
  playPcmData(pcmData, stopS, turnIdx, interrupted) {
    if (!this.outputWorklet) {
      console.warn('AudioProcessor: worklet not initialized');
      return;
    }
    console.debug('AudioProcessor: playing', pcmData.length, 'bytes of PCM data, turnIdx:', turnIdx);

    // Convert Int16LE to Float32
    const int16 = new Int16Array(pcmData.buffer, pcmData.byteOffset, pcmData.byteLength / 2);
    const float32 = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) {
      float32[i] = int16[i] / 32768;
    }

    this.outputWorklet.port.postMessage({
      type: 'audio',
      frame: float32,
      stopS: stopS,
      turnIdx: turnIdx,
      interrupted: interrupted || false,
    });
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
    // Replace decoder on turn change to flush stale state (prevents click after interruption)
    if (packet.turnIdx !== undefined && this._lastDecodedTurnIdx !== undefined
        && packet.turnIdx !== this._lastDecodedTurnIdx) {
      console.debug('AudioProcessor: turn changed', this._lastDecodedTurnIdx, '->', packet.turnIdx, '- replacing decoder');
      this._lastDecodedTurnIdx = packet.turnIdx;
      this._decoderQueue.push(packet);
      this._createDecoder();
      return;
    }
    this._pendingStopS = packet.stopS;
    this._pendingTurnIdx = packet.turnIdx;
    this._lastDecodedTurnIdx = packet.turnIdx;
    this._pendingInterrupted = packet.interrupted || false;

    // Cache Ogg header pages (sent before first audio frame)
    if (!this._oggHeadersCaptured) {
      this._oggHeaderPages.push(new Uint8Array(packet.data));
    }

    const copy = new Uint8Array(packet.data);
    this.decoder.postMessage(
      { command: 'decode', pages: copy },
      [copy.buffer]
    );
  }

  /** @private Create a fresh decoder worker (terminates existing one) */
  _createDecoder() {
    if (this.decoder) {
      this.decoder.terminate();
    }
    this.decoder = new Worker(`${this.basePath}/decoderWorker.min.js`);
    this._decoderReady = false;

    this.decoder.postMessage({
      command: 'init',
      bufferLength: (960 * this.audioContext.sampleRate) / this.sampleRate,
      decoderSampleRate: this.sampleRate,
      outputBufferSampleRate: this.audioContext.sampleRate,
      resampleQuality: 0,
    });

    // Replay cached Ogg headers so the new decoder can parse audio pages
    if (this._oggHeadersCaptured && this._oggHeaderPages.length > 0) {
      console.debug('AudioProcessor: replaying', this._oggHeaderPages.length, 'Ogg header pages to new decoder');
      for (const header of this._oggHeaderPages) {
        const copy = new Uint8Array(header);
        this.decoder.postMessage({ command: 'decode', pages: copy }, [copy.buffer]);
      }
    }

    this._setupDecoderHandlers();

    setTimeout(() => {
      if (!this._decoderReady) {
        console.debug('AudioProcessor: decoder init timeout, marking ready');
        this._decoderReady = true;
        this._flushDecoderQueue();
      }
    }, 500);
  }

  /** @private Wire up onmessage/onerror for current decoder */
  _setupDecoderHandlers() {
    this.decoder.onmessage = (event) => {
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
        // First audio frame means all prior packets were headers
        if (!this._oggHeadersCaptured) {
          // Remove the last entry — it produced audio, so it's not a header
          this._oggHeaderPages.pop();
          this._oggHeadersCaptured = true;
          console.debug('AudioProcessor: captured', this._oggHeaderPages.length, 'Ogg header pages');
        }
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
