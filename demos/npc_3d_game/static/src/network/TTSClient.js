/**
 * One-shot TTS client — sends text, streams OggOpus playback via decoder worker.
 *
 * The backend streams OggOpus chunks over WebSocket.
 * We decode via the decoderWorker (same one AudioProcessor uses) and schedule
 * decoded Float32 frames on a 48kHz AudioContext for gapless playback.
 *
 * This avoids server-side PCM resampling (TTS → Opus is native) and
 * client-side sample rate mismatches (decoder outputs at 48kHz to match context).
 *
 * Usage:
 *   const tts = new TTSClient();
 *   await tts.speak("Hello, Mark.", "Jack");
 */

import { getBasePath, getWsBase } from './basePath.js';

const LOG = '[TTSClient]';
const SAMPLE_RATE = 48000;
const DECODER_SAMPLE_RATE = 24000;

export class TTSClient {
  constructor() {
    this._wsBase = getWsBase();
    console.log(LOG, 'Created, wsBase:', this._wsBase);
  }

  /**
   * Speak text using a Gradium voice. Resolves when playback finishes.
   * @param {string} text
   * @param {string} [voiceName='Emma']
   * @param {object} [opts]
   * @param {() => void} [opts.onFirstAudio] Called when the first audio chunk starts playing
   * @param {number} [opts.speed=1.0] Playback speed multiplier (1.0 = normal)
   * @returns {Promise<void>}
   */
  speak(text, voiceName = 'Emma', opts = {}) {
    return new Promise((resolve) => {
      const { onFirstAudio, speed = 1.0 } = opts;
      console.log(LOG, `speak(): voice=${voiceName}, text="${text.slice(0, 60)}..."`);

      let resolved = false;
      let ws;
      let audioCtx;
      let decoder;
      let nextPlayTime = 0;
      let lastSource = null;
      let chunkCount = 0;
      let firstAudioFired = false;
      let decoderReady = false;
      let pendingChunks = [];
      let wsDone = false;

      const finish = () => {
        if (resolved) return;
        resolved = true;
        clearTimeout(safetyTimer);
        if (decoder) try { decoder.terminate(); } catch (_) {}
        if (audioCtx && audioCtx.state !== 'closed') try { audioCtx.close(); } catch (_) {}
        if (ws && ws.readyState <= WebSocket.OPEN) try { ws.close(); } catch (_) {}
        resolve();
      };

      const safetyTimer = setTimeout(() => {
        console.warn(LOG, 'Safety timeout (30s) — resolving');
        finish();
      }, 30000);

      // 48kHz AudioContext — matches decoder output, no browser resampling needed
      audioCtx = new AudioContext({ sampleRate: SAMPLE_RATE });
      audioCtx.resume().then(() => {
        console.log(LOG, `AudioContext ready: state=${audioCtx.state}, rate=${audioCtx.sampleRate}`);
      });

      // Create OggOpus decoder worker (same one AudioProcessor uses)
      decoder = new Worker(`${getBasePath()}/static/js/decoderWorker.min.js`);
      decoder.postMessage({
        command: 'init',
        bufferLength: Math.round((960 * SAMPLE_RATE) / DECODER_SAMPLE_RATE),
        decoderSampleRate: DECODER_SAMPLE_RATE,
        outputBufferSampleRate: SAMPLE_RATE,
        resampleQuality: 3,
      });

      decoder.onmessage = (event) => {
        if (!decoderReady) {
          decoderReady = true;
          // Flush any chunks that arrived before decoder was ready
          for (const chunk of pendingChunks) {
            const copy = new Uint8Array(chunk);
            decoder.postMessage({ command: 'decode', pages: copy }, [copy.buffer]);
          }
          pendingChunks = [];
        }

        if (!event.data) return;
        const frame = event.data[0];
        if (!frame || frame.length === 0) return;

        if (!firstAudioFired) {
          firstAudioFired = true;
          console.log(LOG, 'First audio decoded');
          if (onFirstAudio) onFirstAudio();
        }

        // Schedule decoded Float32 on AudioContext timeline
        const buffer = audioCtx.createBuffer(1, frame.length, SAMPLE_RATE);
        buffer.getChannelData(0).set(frame);

        const source = audioCtx.createBufferSource();
        source.buffer = buffer;
        source.playbackRate.value = speed;
        source.connect(audioCtx.destination);

        const now = audioCtx.currentTime;
        const startTime = Math.max(now, nextPlayTime);
        source.start(startTime);
        nextPlayTime = startTime + buffer.duration / speed;
        lastSource = source;
      };

      // Decoder init timeout fallback
      setTimeout(() => {
        if (!decoderReady) {
          console.debug(LOG, 'Decoder init timeout, marking ready');
          decoderReady = true;
          for (const chunk of pendingChunks) {
            const copy = new Uint8Array(chunk);
            decoder.postMessage({ command: 'decode', pages: copy }, [copy.buffer]);
          }
          pendingChunks = [];
        }
      }, 500);

      ws = new WebSocket(`${this._wsBase}/ws/tts`);
      ws.binaryType = 'arraybuffer';
      console.log(LOG, 'Connecting to /ws/tts ...');

      ws.onopen = () => {
        console.log(LOG, 'WebSocket connected');
        ws.send(JSON.stringify({ type: 'speak', text, voice_name: voiceName }));
        console.log(LOG, 'Sent speak request');
      };

      ws.onmessage = (event) => {
        // Binary = OggOpus audio chunk
        if (event.data instanceof ArrayBuffer) {
          chunkCount++;
          const bytes = new Uint8Array(event.data);
          if (bytes.length === 0) return;

          if (chunkCount <= 3 || chunkCount % 20 === 0) {
            console.log(LOG, `Opus chunk #${chunkCount}: ${bytes.length} bytes`);
          }

          if (decoderReady) {
            const copy = new Uint8Array(bytes);
            decoder.postMessage({ command: 'decode', pages: copy }, [copy.buffer]);
          } else {
            pendingChunks.push(new Uint8Array(bytes));
          }
          return;
        }

        // JSON message
        const msg = JSON.parse(event.data);
        console.log(LOG, 'JSON:', msg.type);

        if (msg.type === 'done') {
          wsDone = true;
          console.log(LOG, `Done receiving. ${chunkCount} Opus chunks. Audio ends at ${nextPlayTime.toFixed(2)}s`);
          // Give decoder a moment to finish processing last chunks,
          // then wait for the last scheduled audio to finish playing
          setTimeout(() => {
            if (lastSource) {
              lastSource.onended = () => {
                console.log(LOG, 'Last audio chunk finished playing');
                finish();
              };
            } else {
              finish();
            }
          }, 200);
        }
      };

      ws.onerror = (e) => {
        console.error(LOG, 'WebSocket error:', e);
        finish();
      };

      ws.onclose = (e) => {
        console.log(LOG, `WebSocket closed: code=${e.code}`);
        if (!resolved && !wsDone) {
          // Unexpected close — wait for any remaining audio then finish
          if (lastSource) {
            lastSource.onended = () => finish();
          } else {
            finish();
          }
        }
      };
    });
  }
}
