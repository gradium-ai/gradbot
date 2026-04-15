/**
 * Central sound-effects manager.
 * Preloads all one-shot and looping SFX and exposes named play methods.
 */
export class SFXManager {
  constructor() {
    // Looping SFX (footsteps, office hum)
    this._footstepWalk = this._loop('assets/audio/footstep_walk.mp3', 0.25);
    this._footstepSneak = this._loop('assets/audio/footstep_sneak.mp3', 0.15);
    this._officeHum = this._loop('assets/audio/office_hum.mp3', 0.06);

    // One-shot SFX
    this._keypadDeny = this._shot('assets/audio/keypad_deny.mp3', 0.4);
    this._waterCooler = this._shot('assets/audio/water_cooler.mp3', 0.35);
    this._filingCabinet = this._shot('assets/audio/filing_cabinet.mp3', 0.4);
    this._suspicionSting = this._shot('assets/audio/suspicion_sting.mp3', 0.35);
    this._clueChime = this._shot('assets/audio/clue_chime.mp3', 0.45);
    this._uiClick = this._shot('assets/audio/ui_click.mp3', 0.3);

    this._activeFootstep = null; // 'walk' | 'sneak' | null
  }

  _loop(src, vol) {
    const a = new Audio(src);
    a.loop = true;
    a.volume = vol;
    a.preload = 'auto';
    return a;
  }

  _shot(src, vol) {
    const a = new Audio(src);
    a.volume = vol;
    a.preload = 'auto';
    return a;
  }

  _playShot(audio) {
    audio.currentTime = 0;
    audio.play().catch(() => {});
  }

  // ── Footsteps ──────────────────────────────────────────────

  /** Call every frame with the player's current movement state. */
  updateFootsteps(movementState) {
    const moving = movementState === 'walking' || movementState === 'sprinting';
    const sneaking = movementState === 'sneaking';

    if (moving && this._activeFootstep !== 'walk') {
      this._footstepSneak.pause();
      this._footstepWalk.play().catch(() => {});
      // Sprint plays footstep faster
      this._footstepWalk.playbackRate = movementState === 'sprinting' ? 1.4 : 1.0;
      this._activeFootstep = 'walk';
    } else if (sneaking && this._activeFootstep !== 'sneak') {
      this._footstepWalk.pause();
      this._footstepSneak.play().catch(() => {});
      this._activeFootstep = 'sneak';
    } else if (!moving && !sneaking && this._activeFootstep !== null) {
      this._footstepWalk.pause();
      this._footstepSneak.pause();
      this._activeFootstep = null;
    }

    // Keep sprint rate synced
    if (this._activeFootstep === 'walk') {
      this._footstepWalk.playbackRate = movementState === 'sprinting' ? 1.4 : 1.0;
    }
  }

  /** Stop all footstep audio immediately. */
  stopFootsteps() {
    this._footstepWalk.pause();
    this._footstepSneak.pause();
    this._activeFootstep = null;
  }

  // ── Office hum ─────────────────────────────────────────────

  startOfficeHum() {
    this._officeHum.play().catch(() => {});
  }

  stopOfficeHum() {
    this._officeHum.pause();
  }

  // ── One-shots ──────────────────────────────────────────────

  playKeypadDeny() { this._playShot(this._keypadDeny); }
  playWaterCooler() { this._playShot(this._waterCooler); }
  playFilingCabinet() { this._playShot(this._filingCabinet); }
  playSuspicionSting() { this._playShot(this._suspicionSting); }
  playClueChime() { this._playShot(this._clueChime); }
  playUIClick() { this._playShot(this._uiClick); }
}
