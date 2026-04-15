/**
 * Manages three music layers: ambient, suspicion, and MDE (win).
 * Crossfades between ambient ↔ suspicion based on suspicion level,
 * and switches to the MDE track on victory.
 */
export class MusicManager {
  constructor() {
    this._ambient = this._createAudio('assets/audio/ambient.mp3', true, 0);
    this._suspicion = this._createAudio('assets/audio/suspicion.mp3', true, 0);
    this._mde = this._createAudio('assets/audio/mde.mp3', false, 0);
    this._gameover = this._createAudio('assets/audio/gameover.mp3', false, 0);

    this._ambientVol = 0.08;
    this._suspicionVol = 0.12;
    this._mdeVol = 0.25;
    this._gameoverVol = 0.3;

    this._currentFade = null;
    this._started = false;
  }

  _createAudio(src, loop, volume) {
    const audio = new Audio(src);
    audio.loop = loop;
    audio.volume = volume;
    audio.preload = 'auto';
    return audio;
  }

  /** Start ambient music. Call once after game begins. */
  start() {
    if (this._started) return;
    this._started = true;
    this._ambient.volume = 0;
    this._ambient.play().catch(() => {});
    this._suspicion.play().catch(() => {});
    this._fadeTo(this._ambient, this._ambientVol, 2000);
  }

  /**
   * Update music based on suspicion level (0–3).
   * 0 = full ambient, 1+ = crossfade to suspicion.
   */
  setSuspicion(level) {
    if (!this._started || this._mdeActive) return;

    if (level === 0) {
      this._fadeTo(this._ambient, this._ambientVol, 1500);
      this._fadeTo(this._suspicion, 0, 1500);
    } else {
      // Blend: higher suspicion = louder suspicion track
      const t = Math.min(level / 2, 1);
      this._fadeTo(this._ambient, this._ambientVol * (1 - t * 0.7), 1000);
      this._fadeTo(this._suspicion, this._suspicionVol * t, 1000);
    }
  }

  /** Kill all music immediately, then play the game over sting. */
  playGameOver() {
    this._mdeActive = true; // prevent further suspicion changes
    // Hard cut — no fade
    this._ambient.pause();
    this._suspicion.pause();
    this._ambient.volume = 0;
    this._suspicion.volume = 0;

    this._gameover.currentTime = 0;
    this._gameover.volume = 0;
    this._gameover.play().catch(() => {});
    // Fade in after a beat of silence
    setTimeout(() => this._fadeTo(this._gameover, this._gameoverVol, 800), 400);
  }

  /** Crossfade everything to the MDE dance track. */
  playMDE() {
    this._mdeActive = true;
    this._fadeTo(this._ambient, 0, 1500);
    this._fadeTo(this._suspicion, 0, 1500);

    this._mde.currentTime = 0;
    this._mde.volume = 0;
    this._mde.play().catch(() => {});
    this._fadeTo(this._mde, this._mdeVol, 1500);
  }

  /** Fade an audio element to a target volume over duration. */
  _fadeTo(audio, targetVol, durationMs) {
    const startVol = audio.volume;
    const diff = targetVol - startVol;
    if (Math.abs(diff) < 0.01) {
      audio.volume = targetVol;
      return;
    }

    const startTime = performance.now();
    const step = () => {
      const elapsed = performance.now() - startTime;
      const t = Math.min(elapsed / durationMs, 1);
      audio.volume = Math.max(0, Math.min(1, startVol + diff * t));
      if (t < 1) requestAnimationFrame(step);
    };
    requestAnimationFrame(step);
  }
}
