/**
 * AudioWorklet processor for jitter-buffered audio playback.
 *
 * Handles:
 * - Buffering incoming decoded audio frames
 * - Adaptive buffer sizing (increases on underruns)
 * - Packet dropping when buffer exceeds max
 * - Exponential fade-in/out to avoid clicks (including slow fade on interruption)
 * - Delay tracking for metrics
 *
 * Usage:
 *   await audioContext.audioWorklet.addModule('/path/to/audio-output-worklet.js');
 *   const worklet = new AudioWorkletNode(audioContext, 'audio-output-processor');
 *   worklet.connect(audioContext.destination);
 *
 *   // Send decoded audio frames:
 *   worklet.port.postMessage({ type: 'audio', frame: float32Array });
 *
 *   // Reset state:
 *   worklet.port.postMessage({ type: 'reset' });
 */

function asMs(samples) {
  return ((samples * 1000) / sampleRate).toFixed(1);
}

function asSamples(ms) {
  return Math.round((ms * sampleRate) / 1000);
}

// Maximum buffer before we start dropping packets (30 seconds)
const DEFAULT_MAX_BUFFER_MS = 30 * 1000;

// Fade durations in ms
const FADE_IN_MS = 15;
const FADE_OUT_MS = 10;
const FADE_OUT_INTERRUPTED_MS = 200;

// Exponential fade threshold — fade is "complete" when it reaches this level (~-60dB)
const FADE_THRESHOLD = 0.001;

// Compute per-sample exponential decay factor for a given duration in ms.
// After N samples, the fade reaches FADE_THRESHOLD (effectively silent).
function fadeDecay(ms) {
  const N = asSamples(ms);
  return Math.pow(FADE_THRESHOLD, 1.0 / Math.max(1, N));
}

// Set to true to enable debug logging
const DEBUG = false;
const debug = (...args) => {
  if (DEBUG) console.debug('[AudioOutputWorklet]', ...args);
};

class AudioOutputProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    debug('AudioOutputProcessor created', currentFrame, sampleRate);

    // Buffer timing configuration (in samples)
    const frameSize = asSamples(80);

    // Wait for at least this many samples before starting playback
    this.initialBufferSamples = 1 * frameSize;

    // Additional wait after reaching initialBufferSamples (allows non-frame-aligned buffers)
    this.partialBufferSamples = asSamples(10);

    // Drop oldest packets if buffer exceeds this
    this.maxBufferSamples = asSamples(DEFAULT_MAX_BUFFER_MS);

    // How much to increase buffers on underrun/overflow
    this.partialBufferIncrement = asSamples(5);
    this.maxPartialWithIncrements = asSamples(80);
    this.maxBufferSamplesIncrement = asSamples(5);
    this.maxMaxBufferWithIncrements = asSamples(80);

    this.initState();

    this.port.onmessage = (event) => {
      if (event.data.type === 'reset') {
        debug('Reset audio processor state');
        this.initState();
        return;
      }

      if (event.data.type === 'audio') {
        const frame = event.data.frame;
        const stopS = event.data.stopS;
        const turnIdx = event.data.turnIdx;

        console.log(
          '[AudioWorklet] Audio received:',
          frame.length + ' samples (' + asMs(frame.length) + 'ms),',
          'turn=' + turnIdx,
          'stopS=' + stopS
        );

        // Route audio to correct buffer based on turn
        let buffered = false;
        if (turnIdx === this.currentPlayingTurnIdx || this.currentPlayingTurnIdx === null) {
          // Current turn - add to main buffer
          this.pushFrame(this.frames, frame, turnIdx);
          if (this.currentPlayingTurnIdx === null) {
            this.currentPlayingTurnIdx = turnIdx;
          }
          buffered = true;
          debug('Audio to MAIN buffer - turn', turnIdx, 'frames now:', this.frames.length);
        } else if (turnIdx > this.currentPlayingTurnIdx) {
          // Future turn - check if we can play immediately (buffer is empty)
          if (this.frames.length === 0) {
            // Buffer is empty, play new turn immediately
            this.currentPlayingTurnIdx = turnIdx;
            this.pushFrame(this.frames, frame, turnIdx);
            buffered = true;
            debug('Buffer empty - starting new turn immediately', turnIdx);
            console.log('[AudioWorklet] Starting turn', turnIdx, 'immediately (buffer was empty)');
          } else {
            // Buffer not empty - add to pending buffer
            // If we get a newer turn than what's pending, replace pending (skip old turn)
            if (this.pendingTurnIdx === null || turnIdx > this.pendingTurnIdx) {
              if (this.pendingTurnIdx !== null) {
                console.log('[AudioWorklet] Skipping turn', this.pendingTurnIdx, 'for newer turn', turnIdx);
              }
              debug('New pending turn', this.pendingTurnIdx, '->', turnIdx, '- clearing old pending');
              this.pendingFrames = [];
              this.pendingTurnIdx = turnIdx;
            }
            // Add to pending buffer (either new or existing pending)
            if (turnIdx === this.pendingTurnIdx) {
              this.pushFrame(this.pendingFrames, frame, turnIdx);
              buffered = true;
              debug('Audio to PENDING buffer - turn', turnIdx, 'pending frames now:', this.pendingFrames.length);
            }
            // If turnIdx < pendingTurnIdx, it's stale - drop it
          }
        } else {
          // Stale turn - drop it
          debug('DROPPING audio - stale turn', turnIdx, '< current', this.currentPlayingTurnIdx);
          console.log('[AudioWorklet] DROPPING audio for stale turn', turnIdx, '(current:', this.currentPlayingTurnIdx + ')');
        }

        // Only update tracking/state for audio that was actually buffered
        if (buffered) {
          if (stopS !== undefined) {
            this.lastStopS = stopS;
          }

          // Handle interrupted flag — switch to slow fade-out
          // Only for current-turn audio (not pending), since it affects the active playback fade
          if (event.data.interrupted && turnIdx === this.currentPlayingTurnIdx) {
            const currentBuffered = this.currentSamples();
            const desiredFade = asSamples(FADE_OUT_INTERRUPTED_MS);
            const effectiveFade = Math.min(desiredFade, currentBuffered);
            const effectiveFadeMs = (effectiveFade * 1000) / sampleRate;
            this.fadeOutDecay = fadeDecay(effectiveFadeMs);
            console.log(
              '[AudioWorklet] Interrupted fade-out:',
              'buffered=' + asMs(currentBuffered) + 'ms,',
              'fade=' + asMs(effectiveFade) + 'ms,',
              'decay=' + this.fadeOutDecay.toFixed(6)
            );
          }
        }

        // Start playback once we have enough buffered
        if (this.currentSamples() >= this.initialBufferSamples && !this.started) {
          this.start();
        }

        if (this.pidx < 20) {
          debug(
            this.timestamp(),
            'Got packet',
            this.pidx++,
            'turn=' + turnIdx,
            'current=' + this.currentPlayingTurnIdx,
            'pending=' + this.pendingTurnIdx,
            asMs(this.currentSamples()) + 'ms buffered',
            asMs(frame.length) + 'ms frame'
          );
        }

        // Drop packets if buffer is too full
        if (this.currentSamples() >= this.totalMaxBufferSamples()) {
          this.dropExcessPackets();
        }

        // Send metrics back to main thread (bufferMs and lastStopS are atomic)
        this.port.postMessage({
          type: 'metrics',
          totalAudioPlayed: this.totalAudioPlayed,
          actualAudioPlayed: this.actualAudioPlayed,
          bufferMs: (this.currentSamples() / sampleRate) * 1000,
          lastStopS: this.lastStopS,
          minDelay: this.minDelay,
          maxDelay: this.maxDelay,
        });
      }
    };
  }

  initState() {
    this.frames = [];
    this.offsetInFirstBuffer = 0;
    this.remainingPartialBufferSamples = 0;
    this.fadePos = 0; // 0.0..1.0, current fade multiplier
    this.fadeInDecay = fadeDecay(FADE_IN_MS);   // per-sample decay for fade-in
    this.fadeOutDecay = fadeDecay(FADE_OUT_MS);  // per-sample decay for fade-out
    this.timeInStream = 0;
    this.resetStart();

    // Metrics
    this.totalAudioPlayed = 0;
    this.actualAudioPlayed = 0;
    this.maxDelay = 0;
    this.minDelay = 2000;
    this.lastStopS = 0; // stop_s of the last audio frame added to buffer

    // Turn tracking
    this.currentPlayingTurnIdx = null;

    // Buffer empty detection
    this.hadFrames = false;

    // Pending buffer for next turn (prevents mixing audio between turns)
    this.pendingFrames = [];
    this.pendingTurnIdx = null;

    // Debug counter
    this.pidx = 0;

    // Reset buffer params
    this.partialBufferSamples = asSamples(10);
    this.maxBufferSamples = asSamples(DEFAULT_MAX_BUFFER_MS);
  }

  totalMaxBufferSamples() {
    return this.maxBufferSamples + this.partialBufferSamples + this.initialBufferSamples;
  }

  timestamp() {
    return Date.now() % 1000;
  }

  currentSamples() {
    let samples = 0;
    for (let k = 0; k < this.frames.length; k++) {
      samples += this.frames[k].samples.length;
    }
    samples -= this.offsetInFirstBuffer;
    return samples;
  }

  resetStart() {
    this.started = false;
  }

  start() {
    this.started = true;
    this.remainingPartialBufferSamples = this.partialBufferSamples;
  }

  canPlay() {
    return this.started && this.frames.length > 0 && this.remainingPartialBufferSamples <= 0;
  }

  pushFrame(buffer, frame, turnIdx) {
    buffer.push({ samples: frame, turnIdx: turnIdx });
  }

  dropExcessPackets() {
    debug(
      this.timestamp(),
      'Dropping packets',
      asMs(this.currentSamples()) + 'ms buffered',
      asMs(this.totalMaxBufferSamples()) + 'ms max'
    );

    const target = this.initialBufferSamples + this.partialBufferSamples;
    while (this.currentSamples() > target) {
      const first = this.frames[0];
      let toRemove = this.currentSamples() - target;
      toRemove = Math.min(first.samples.length - this.offsetInFirstBuffer, toRemove);
      this.offsetInFirstBuffer += toRemove;
      this.timeInStream += toRemove / sampleRate;

      if (this.offsetInFirstBuffer === first.samples.length) {
        this.frames.shift();
        this.offsetInFirstBuffer = 0;
      }
    }

    debug(this.timestamp(), 'After drop:', asMs(this.currentSamples()) + 'ms');

    // Increase max buffer to reduce future drops
    this.maxBufferSamples += this.maxBufferSamplesIncrement;
    this.maxBufferSamples = Math.min(this.maxMaxBufferWithIncrements, this.maxBufferSamples);
    debug('Increased maxBuffer to', asMs(this.maxBufferSamples) + 'ms');
  }

  process(inputs, outputs) {
    const delay = this.currentSamples() / sampleRate;
    if (this.canPlay()) {
      this.maxDelay = Math.max(this.maxDelay, delay);
      this.minDelay = Math.min(this.minDelay, delay);
    }

    const output = outputs[0][0];

    if (!this.canPlay()) {
      if (this.actualAudioPlayed > 0) {
        this.totalAudioPlayed += output.length / sampleRate;
      }
      this.fadePos = 0;
      this.remainingPartialBufferSamples -= output.length;
      // Output near-silent signal to prevent Chrome from suspending the audio output
      // path (which causes an audible "switch" sound when audio resumes).
      for (let i = 0; i < output.length; i++) {
        output[i] = 1e-7;
      }
      return true;
    }

    let outIdx = 0;

    // Copy frames to output buffer
    // Note: All frames in this.frames now have the same turnIdx due to pending buffer system
    while (outIdx < output.length && this.frames.length) {
      const first = this.frames[0];

      const toCopy = Math.min(
        first.samples.length - this.offsetInFirstBuffer,
        output.length - outIdx
      );
      const subArray = first.samples.subarray(
        this.offsetInFirstBuffer,
        this.offsetInFirstBuffer + toCopy
      );
      output.set(subArray, outIdx);

      this.offsetInFirstBuffer += toCopy;
      outIdx += toCopy;

      if (this.offsetInFirstBuffer === first.samples.length) {
        this.offsetInFirstBuffer = 0;
        this.frames.shift();
      }
    }

    // Apply per-sample exponential fade based on buffer fullness.
    // fadePos (0.0..1.0) ramps up/down exponentially.
    // Fade-out starts when remaining samples < samples needed to reach FADE_THRESHOLD.
    const remaining = this.currentSamples();
    // How many samples needed for fadePos to decay to FADE_THRESHOLD:
    //   fadePos * fadeOutDecay^N = FADE_THRESHOLD  =>  N = log(FADE_THRESHOLD/fadePos) / log(fadeOutDecay)
    const fadeOutSamplesNeeded = this.fadePos > FADE_THRESHOLD
      ? Math.log(FADE_THRESHOLD / this.fadePos) / Math.log(this.fadeOutDecay)
      : 0;
    const willFadeOut = remaining + outIdx < fadeOutSamplesNeeded;
    const alreadyFull = this.fadePos >= 1 - FADE_THRESHOLD;

    if (!alreadyFull || willFadeOut) {
      const fadePosStart = this.fadePos;
      for (let i = 0; i < outIdx; i++) {
        const samplesLeft = remaining + (outIdx - i);
        // Re-check fade-out condition per sample (fadePos changes each iteration)
        const neededNow = this.fadePos > FADE_THRESHOLD
          ? Math.log(FADE_THRESHOLD / this.fadePos) / Math.log(this.fadeOutDecay)
          : 0;
        if (samplesLeft < neededNow) {
          // Fade out: exponential decay toward 0
          this.fadePos *= this.fadeOutDecay;
          if (this.fadePos < FADE_THRESHOLD) this.fadePos = 0;
        } else {
          // Fade in: exponential approach toward 1
          // fadePos = 1 - (1 - fadePos) * decay
          this.fadePos = 1 - (1 - this.fadePos) * this.fadeInDecay;
          if (this.fadePos > 1 - FADE_THRESHOLD) this.fadePos = 1;
        }
        output[i] *= this.fadePos;
      }
      console.log(
        '[AudioWorklet] Fade active:',
        fadePosStart.toFixed(3), '->', this.fadePos.toFixed(3),
        willFadeOut ? 'fade-out' : 'fade-in',
        'remaining=' + asMs(remaining) + 'ms',
        'outIdx=' + outIdx,
        'fadeOutDecay=' + this.fadeOutDecay.toFixed(6)
      );
    }

    // Handle buffer underrun
    if (outIdx < output.length) {
      debug(this.timestamp(), 'Buffer underrun:', output.length - outIdx, 'samples missed');

      // Increase buffer to reduce future underruns
      this.partialBufferSamples += this.partialBufferIncrement;
      this.partialBufferSamples = Math.min(this.partialBufferSamples, this.maxPartialWithIncrements);
      debug('Increased partial buffer to', asMs(this.partialBufferSamples) + 'ms');

      // Reset to refill buffer
      this.resetStart();
    }

    this.totalAudioPlayed += output.length / sampleRate;
    this.actualAudioPlayed += outIdx / sampleRate;
    this.timeInStream += outIdx / sampleRate;

    // Track buffer state and check for pending buffer swap
    if (this.frames.length > 0) {
      this.hadFrames = true;
    } else {
      // Main buffer is empty - check if we have pending audio to swap in
      if (this.pendingFrames.length > 0) {
        const oldTurnIdx = this.currentPlayingTurnIdx;
        const newTurnIdx = this.pendingTurnIdx;
        debug('Buffer empty - swapping pending turn', oldTurnIdx, '->', newTurnIdx, 
              'with', this.pendingFrames.length, 'frames');
        
        // Notify main thread of turn change
        if (oldTurnIdx !== null) {
          this.port.postMessage({
            type: 'turn_change',
            oldTurnIdx: oldTurnIdx,
            newTurnIdx: newTurnIdx,
          });
        }
        
        // Swap pending to main
        this.frames = this.pendingFrames;
        this.pendingFrames = [];
        this.currentPlayingTurnIdx = newTurnIdx;
        this.pendingTurnIdx = null;
        this.hadFrames = true;

        // Reset fade for new turn — start from silence
        this.fadePos = 0;
        this.fadeOutDecay = fadeDecay(FADE_OUT_MS);

        // Restart playback for the new turn
        this.started = true;
        this.remainingPartialBufferSamples = 0;
        console.log('[AudioWorklet] Swapped to turn', newTurnIdx, 'and restarted playback');
      } else if (this.hadFrames) {
        // Buffer just emptied with no pending audio — reset fade for next turn
        this.hadFrames = false;
        this.fadeOutDecay = fadeDecay(FADE_OUT_MS);
        this.port.postMessage({
          type: 'metrics',
          totalAudioPlayed: this.totalAudioPlayed,
          actualAudioPlayed: this.actualAudioPlayed,
          bufferMs: 0,
          lastStopS: this.lastStopS,
          minDelay: this.minDelay,
          maxDelay: this.maxDelay,
        });
      }
    }

    return true;
  }
}

registerProcessor('audio-output-processor', AudioOutputProcessor);
