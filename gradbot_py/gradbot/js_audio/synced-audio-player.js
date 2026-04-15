/**
 * SyncedAudioPlayer - Handles audio playback with synchronized text display.
 *
 * This wrapper abstracts the complexity of syncing text display with audio playback.
 * It receives WebSocket messages (audio + text) and emits text events at the right time.
 *
 * Usage:
 *   const player = new SyncedAudioPlayer({
 *     basePath: '/static/js',
 *     sampleRate: 24000,
 *     echoCancellation: true,
 *     onEncodedAudio: (opusData) => ws.send(opusData),
 *     onText: ({ text, turnIdx, startS, stopS, isUser, character }) => {
 *       // Display text now - audio just finished playing
 *     },
 *     onEvent: (event) => { ... },
 *     onError: (error) => { ... }
 *   });
 *
 *   await player.start();
 *   ws.onmessage = (event) => player.handleMessage(event.data);
 *   player.stop();
 */

class SyncedAudioPlayer {
  constructor(options) {
    this.basePath = options.basePath || '/js';
    this.sampleRate = options.sampleRate || 24000;
    this.echoCancellation = options.echoCancellation !== false;
    this.onEncodedAudio = options.onEncodedAudio || (() => {});
    this.onText = options.onText || (() => {});
    this.onEvent = options.onEvent || (() => {});
    this.onError = options.onError || ((e) => console.error('SyncedAudioPlayer error:', e));
    this.onEndOfTurn = options.onEndOfTurn || (() => {}); // Called when turn audio finishes playing
    this.pcmOutput = options.pcmOutput || false; // Use PCM instead of Opus for incoming audio

    this.audioProcessor = null;
    this._flushInterval = null;
    this._endOfTurnTimeout = null;
    this._started = false;
    this._messageBuffer = []; // Buffer messages received before start()
    this._processingChain = Promise.resolve(); // Serializes async message processing

    // Timing state
    // lastStopS = timestamp at END of buffer (latest audio that arrived)
    // bufferMs = duration of audio in buffer at time of last metric
    // lastMetricTime = when we received the last metric
    // playhead position = lastStopS - bufferMs/1000 + elapsed time since metric
    this.bufferMs = 0;
    this.lastStopS = null;
    this.lastMetricTime = null;
    this.pendingAudioStopS = null;
    this.pendingAudioTurnIdx = null;
    this.pendingAudioInterrupted = false;
    this.currentTurnIdx = null;
    this.pendingTexts = []; // Queue of { text, startS, stopS, turnIdx, isUser, character }
  }

  async start() {
    this.audioProcessor = new AudioProcessor({
      basePath: this.basePath,
      sampleRate: this.sampleRate,
      echoCancellation: this.echoCancellation,
      onEncodedAudio: (opusData) => {
        this.onEncodedAudio(opusData);
      },
      onMetrics: (metrics) => {
        // Just update state - the flush interval handles text display
        this.bufferMs = metrics.bufferMs || 0;
        this.lastMetricTime = Date.now();

        if (metrics.lastStopS !== undefined) {
          this.lastStopS = metrics.lastStopS;
        }
      },
      onTurnChange: ({ oldTurnIdx, newTurnIdx }) => {
        // Turn change at playhead
        // Flush any remaining texts from the old turn
        this._flushAllTexts();
      },
    });

    await this.audioProcessor.start();

    // Start flush interval - check every 50ms what texts are ready to display
    this._flushInterval = setInterval(() => {
      this._flushReadyTexts();
    }, 50);

    this._started = true;

    // Replay any messages that arrived before start()
    if (this._messageBuffer.length > 0) {
      // Replaying buffered messages
      for (const data of this._messageBuffer) {
        await this._processMessage(data);
      }
      this._messageBuffer = [];
    }
  }

  /**
   * Wait for all queued messages to finish processing.
   */
  drain() {
    return this._processingChain;
  }

  stop() {
    if (this._flushInterval) {
      clearInterval(this._flushInterval);
      this._flushInterval = null;
    }
    if (this.audioProcessor) {
      this.audioProcessor.stop();
      this.audioProcessor = null;
    }
    this._reset();
    this._started = false;
    this._messageBuffer = [];
    this._processingChain = Promise.resolve();
  }

  _reset() {
    this.bufferMs = 0;
    this.lastStopS = null;
    this.lastMetricTime = null;
    this.pendingAudioStopS = null;
    this.pendingAudioTurnIdx = null;
    this.currentTurnIdx = null;
    this.pendingTexts = [];
    if (this._endOfTurnTimeout) {
      clearTimeout(this._endOfTurnTimeout);
      this._endOfTurnTimeout = null;
    }
  }

  /**
   * Get the current playhead position in seconds.
   * This is where audio is currently being played.
   * We estimate it based on last known buffer state + elapsed time.
   */
  _getPlayheadS() {
    if (this.lastStopS === null || this.lastMetricTime === null) return null;
    const elapsedS = (Date.now() - this.lastMetricTime) / 1000;
    // playhead = end of buffer - buffer length + time elapsed since we measured
    return this.lastStopS - this.bufferMs / 1000 + elapsedS;
  }

  /**
   * Get the current turn index.
   */
  getTurnIdx() {
    return this.currentTurnIdx;
  }

  /**
   * Public method to schedule onEndOfTurn callback for when buffered audio finishes.
   * Call this when you know the turn is ending (e.g., after receiving end_tts_audio).
   */
  scheduleEndOfTurn() {
    this._scheduleEndOfTurn(this.currentTurnIdx);
  }

  /**
   * Schedule the onEndOfTurn callback for when the current turn's audio finishes playing.
   * We calculate: time until buffer drains = lastStopS - playheadS
   */
  _scheduleEndOfTurn(turnIdx) {
    // Clear any existing scheduled callback
    if (this._endOfTurnTimeout) {
      clearTimeout(this._endOfTurnTimeout);
      this._endOfTurnTimeout = null;
    }

    const playheadS = this._getPlayheadS();
    if (playheadS === null || this.lastStopS === null) {
      // No timing info, fire immediately
      // End of turn (immediate, no timing)
      this._flushAllTexts();
      this.onEndOfTurn(turnIdx);
      return;
    }

    // Time until all buffered audio finishes = lastStopS - playheadS
    const remainingS = Math.max(0, this.lastStopS - playheadS);
    const delayMs = remainingS * 1000;

    // Scheduling end of turn

    this._endOfTurnTimeout = setTimeout(() => {
      this._endOfTurnTimeout = null;
      // End of turn fired
      this._flushAllTexts();
      this.onEndOfTurn(turnIdx);
    }, delayMs);
  }

  /**
   * Handle a WebSocket message (Blob or parsed JSON).
   * Call this from ws.onmessage.
   * Can be called before start() - messages will be buffered and replayed.
   */
  handleMessage(data) {
    if (!this._started) {
      // Buffer messages until start() completes
      this._messageBuffer.push(data);
      return;
    }
    // Chain onto previous processing to ensure messages are handled sequentially.
    // Without this, concurrent Blob.arrayBuffer() awaits cause audio_timing/blob pairs
    // to interleave, corrupting pendingAudioStopS/pendingAudioTurnIdx and triggering
    // a cascade of decoder recreations that freezes the browser.
    this._processingChain = this._processingChain.then(() => this._processMessage(data));
  }

  /** @private Process a message (after start) */
  async _processMessage(data) {
    try {
      if (data instanceof Blob) {
        // Audio binary - play with pending timing info
        const arrayBuffer = await data.arrayBuffer();
        if (this.audioProcessor) {
          const bytes = new Uint8Array(arrayBuffer);
          if (this.pcmOutput) {
            this.audioProcessor.playPcmData(
              bytes,
              this.pendingAudioStopS,
              this.pendingAudioTurnIdx,
              this.pendingAudioInterrupted
            );
          } else {
            this.audioProcessor.playOpusData(
              bytes,
              this.pendingAudioStopS,
              this.pendingAudioTurnIdx,
              this.pendingAudioInterrupted
            );
          }
        }
        this.pendingAudioStopS = null;
        this.pendingAudioTurnIdx = null;
        this.pendingAudioInterrupted = false;
        // Flush ready texts after processing audio
        this._flushReadyTexts();
      } else {
        // JSON message
        const msg = typeof data === 'string' ? JSON.parse(data) : data;
        this._handleJsonMessage(msg);
      }
    } catch (e) {
      this.onError(e);
    }
  }

  _handleJsonMessage(msg) {
    switch (msg.type) {
      case 'audio_timing':
        this._handleAudioTiming(msg);
        break;

      case 'user_text':
      case 'agent_text':
        this._handleTranscript(msg, msg.type === 'user_text');
        break;

      case 'event':
        // Handle end_of_turn - schedule callback for when audio finishes
        if (msg.event === 'end_of_turn') {
          this._scheduleEndOfTurn(this.currentTurnIdx);
        }
        this.onEvent(msg.event, msg);
        break;

      case 'error':
        this.onError(new Error(msg.message));
        break;

      default:
        // Pass through unknown message types via onEvent
        this.onEvent(msg.type, msg);
        break;
    }
    // Flush ready texts after any JSON message
    this._flushReadyTexts();
  }

  _handleAudioTiming(msg) {
    const turnIdx = msg.turn_idx;

    // audio_timing received

    this.currentTurnIdx = turnIdx;
    this.pendingAudioStopS = msg.stop_s;
    this.pendingAudioTurnIdx = turnIdx;
    this.pendingAudioInterrupted = msg.interrupted || false;
  }

  _handleTranscript(msg, isUser) {
    if (isUser) {
      // User transcript (immediate)
      // User transcript - emit immediately
      this._emitText({
        text: msg.text,
        turnIdx: msg.turn_idx,
        startS: msg.start_s,
        stopS: msg.stop_s,
        isUser: true,
        character: msg.character,
      });
      return;
    }

    // Assistant transcript - queue for timed display
    const textItem = {
      text: msg.text,
      turnIdx: msg.turn_idx,
      startS: msg.start_s,
      stopS: msg.stop_s,
      isUser: false,
      character: msg.character,
    };

    // Queuing text for timed display

    // Add to queue (flush happens after _handleJsonMessage returns)
    this.pendingTexts.push(textItem);
  }

  _emitText(item) {
    this.onText({
      text: item.text,
      turnIdx: item.turnIdx,
      startS: item.startS,
      stopS: item.stopS,
      isUser: item.isUser,
      character: item.character,
    });
  }

  /**
   * Flush texts whose audio is about to be played.
   * Condition: item.stopS <= playheadS + leeway
   * The leeway (150ms) ensures text appears slightly before audio finishes,
   * which feels more natural and prevents missing the last word.
   */
  _flushReadyTexts() {
    const playheadS = this._getPlayheadS();
    if (playheadS === null) {
      // No timing info yet, can't determine what's ready
      return;
    }

    const LEEWAY_S = 0.15; // Show text 150ms before audio finishes
    const toEmit = [];
    const toKeep = [];

    for (const item of this.pendingTexts) {
      if (item.stopS <= playheadS + LEEWAY_S) {
        toEmit.push(item);
      } else {
        toKeep.push(item);
      }
    }

    if (toEmit.length > 0) {
      // Flushing ready texts
    }

    this.pendingTexts = toKeep;

    // Emit in order
    for (const item of toEmit) {
      // Emit text
      this._emitText(item);
    }
  }

  /**
   * Flush all pending texts immediately (used on turn change or buffer drain).
   */
  _flushAllTexts() {
    if (this.pendingTexts.length === 0) return;

    // Flushing all pending texts
    while (this.pendingTexts.length > 0) {
      const item = this.pendingTexts.shift();
      // Flush all - emit
      this._emitText(item);
    }
  }
}

// Export for ES modules, or attach to window for script tags
if (typeof module !== 'undefined' && module.exports) {
  module.exports = SyncedAudioPlayer;
} else if (typeof window !== 'undefined') {
  window.SyncedAudioPlayer = SyncedAudioPlayer;
}
