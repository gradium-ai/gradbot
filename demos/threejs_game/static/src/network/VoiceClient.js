/**
 * WebSocket + audio client for gradbot voice sessions.
 *
 * Uses AudioProcessor (from gradbot's js_audio_processor) for both:
 *   - Mic capture (Opus encoding → send to server)
 *   - Audio playback (OggOpus decoding via decoder worker + AudioWorklet)
 *
 * Single 48kHz AudioContext handles everything — no sample rate mismatch.
 *
 * Supports two session types:
 *   - Clue sessions:    vc.connect('note')     → /ws/clue/note
 *   - Check-in sessions: vc.connectCheckin()   → /ws/checkin
 *
 * Usage:
 *   const vc = new VoiceClient({ onTranscript, onClueResult, onCheckinResult });
 *   await vc.connect('note');   // opens WS + mic for clue
 *   vc.disconnect();            // closes everything
 */

import { getBasePath, getWsBase } from './basePath.js';

const LOG = '[VoiceClient]';

export class VoiceClient {
  /**
   * @param {object} opts
   * @param {(text: string, isUser: boolean) => void} opts.onTranscript
   * @param {(correct: boolean, fragment: string|null) => void} opts.onClueResult
   * @param {(classification: string, reason: string) => void} [opts.onCheckinResult]
   * @param {(err: string) => void} [opts.onError]
   * @param {string} [opts.backendUrl]
   */
  constructor(opts) {
    this._onTranscript = opts.onTranscript;
    this._onClueResult = opts.onClueResult;
    this._onCheckinResult = opts.onCheckinResult || (() => {});
    this._onError = opts.onError || ((e) => console.error(LOG, 'error:', e));

    this._wsBase = opts.backendUrl || getWsBase();

    /** @type {WebSocket|null} */
    this._ws = null;
    /** @type {AudioProcessor|null} */
    this._audio = null;
    this._connected = false;

    // Pending audio timing from audio_timing JSON (consumed by next binary frame)
    this._pendingStopS = null;
    this._pendingTurnIdx = null;
    this._pendingInterrupted = false;
  }

  get connected() { return this._connected; }

  /**
   * Open a voice session for a clue.
   * @param {string} clueId
   */
  async connect(clueId) {
    await this._connectToPath(`/ws/clue/${clueId}`);
  }

  /**
   * Open a voice session for a Milchick check-in.
   */
  async connectCheckin() {
    await this._connectToPath('/ws/checkin');
  }

  /**
   * Connect to a gradbot voice WebSocket at the given path.
   * Sets up AudioProcessor for mic capture and OggOpus playback.
   * If already connected, disconnects the current session first.
   * @param {string} wsPath
   */
  async _connectToPath(wsPath) {
    if (this._connected) {
      console.warn(LOG, 'Already connected — disconnecting before new session');
      this.disconnect();
    }

    if (typeof AudioProcessor === 'undefined') {
      throw new Error('AudioProcessor not loaded. Check script tags in index.html.');
    }

    const url = `${this._wsBase}${wsPath}`;
    console.log(LOG, 'Connecting to', url);
    this._ws = new WebSocket(url);
    this._ws.binaryType = 'arraybuffer';

    await new Promise((resolve, reject) => {
      this._ws.onopen = () => { console.log(LOG, 'WebSocket connected'); resolve(); };
      this._ws.onerror = () => reject(new Error('WebSocket connection failed'));
    });

    // Send start message (required by gradbot fastapi protocol)
    this._ws.send(JSON.stringify({ type: 'start' }));
    console.log(LOG, 'Sent start message');

    // Set up AudioProcessor for mic capture AND OggOpus playback
    // Single 48kHz AudioContext handles both directions
    this._audio = new AudioProcessor({
      basePath: `${getBasePath()}/static/js`,
      echoCancellation: true,
      onEncodedAudio: (data) => {
        if (this._ws && this._ws.readyState === WebSocket.OPEN) {
          this._ws.send(data);
        }
      },
    });

    await this._audio.start();

    // Set up message handling
    this._ws.onmessage = (event) => this._handleMessage(event);
    this._ws.onclose = (e) => {
      console.log(LOG, `WebSocket closed: code=${e.code}`);
      this._connected = false;
    };
    this._ws.onerror = () => this._onError('WebSocket error');

    this._connected = true;
    console.log(LOG, 'Connected and mic active');
  }

  /**
   * Handle incoming WebSocket messages.
   * Binary = OggOpus audio; JSON = control messages.
   * audio_timing JSON always arrives immediately before its binary audio frame.
   * @param {MessageEvent} event
   */
  _handleMessage(event) {
    // Binary = OggOpus audio chunk — decode + play via AudioProcessor
    if (event.data instanceof ArrayBuffer) {
      const bytes = new Uint8Array(event.data);
      if (bytes.length === 0) return;

      if (this._audio) {
        this._audio.playOpusData(
          bytes,
          this._pendingStopS,
          this._pendingTurnIdx,
          this._pendingInterrupted,
        );
      }
      this._pendingStopS = null;
      this._pendingTurnIdx = null;
      this._pendingInterrupted = false;
      return;
    }

    const msg = JSON.parse(event.data);
    console.log(LOG, 'JSON:', msg.type);

    switch (msg.type) {
      case 'audio_timing':
        // Stash timing — will be attached to the next binary audio frame
        this._pendingStopS = msg.stop_s;
        this._pendingTurnIdx = msg.turn_idx;
        this._pendingInterrupted = msg.interrupted || false;
        break;
      case 'user_text':
      case 'agent_text':
        this._onTranscript(msg.text, msg.type === 'user_text');
        break;
      case 'clue_result':
        this._onClueResult(msg.correct, msg.fragment, msg.attempts_exhausted);
        break;
      case 'checkin_result':
        if (this._onCheckinResultOnce) {
          this._onCheckinResultOnce(msg.classification);
        }
        this._onCheckinResult(msg.classification, msg.reason);
        break;
      case 'error':
        this._onError(msg.message || 'Unknown backend error');
        break;
      case 'event':
        console.log(LOG, 'Event:', msg.event);
        break;
    }
  }

  /**
   * Returns a Promise that resolves with the check-in classification.
   * Rejects on timeout.
   * @param {number} timeoutMs
   * @returns {Promise<string>}
   */
  waitForCheckinResult(timeoutMs = 15000) {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this._onCheckinResultOnce = null;
        reject(new Error('Check-in timeout'));
      }, timeoutMs);

      this._onCheckinResultOnce = (classification) => {
        clearTimeout(timer);
        this._onCheckinResultOnce = null;
        resolve(classification || 'nervous');
      };
    });
  }

  /**
   * Disconnect and release all resources.
   */
  disconnect() {
    console.log(LOG, 'Disconnecting');
    if (this._audio) {
      this._audio.stop();
      this._audio = null;
    }
    if (this._ws) {
      if (this._ws.readyState === WebSocket.OPEN) {
        this._ws.send(JSON.stringify({ type: 'stop' }));
      }
      this._ws.close();
      this._ws = null;
    }
    this._connected = false;
    this._pendingStopS = null;
    this._pendingTurnIdx = null;
    this._pendingInterrupted = false;
  }
}
