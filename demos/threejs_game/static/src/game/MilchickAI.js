import * as THREE from 'three';
import { CONFIG } from '../config.js';

/**
 * Milchick check-in state machine.
 *
 * States:
 *   idle        — waiting at home position, timer ticking toward next check-in
 *   approaching — warning cue shown, walk anim plays, Milchick moves toward player
 *   checking_in — clue interaction paused, voice session with player
 *   leaving     — Milchick walks back to home position
 *   alert       — suspicion reached max, game over triggered
 *
 * Movement is now distance-based (constant speed) instead of time-based,
 * preventing the skating/gliding effect.
 */

const AI = () => CONFIG.MILCHICK_AI;


function _wait(ms) {
  return new Promise(r => setTimeout(r, ms));
}

function _randomRange(min, max) {
  return min + Math.random() * (max - min);
}

export class MilchickAI {
  /**
   * @param {object} deps
   * @param {import('./Milchick.js').Milchick} deps.milchick
   * @param {import('../ui/GameUI.js').GameUI} deps.gameUI
   * @param {import('./SuspicionSystem.js').SuspicionSystem} deps.suspicion
   * @param {import('./InteractionSystem.js').InteractionSystem} deps.interactionSystem
   * @param {import('../network/TTSClient.js').TTSClient} deps.ttsClient
   * @param {import('../network/VoiceClient.js').VoiceClient} deps.voiceClient
   * @param {THREE.Object3D} deps.playerModel
   * @param {HTMLElement} deps.canvas  The renderer canvas (for re-acquiring pointer lock)
   */
  constructor(deps) {
    this._milchick = deps.milchick;
    this._ui = deps.gameUI;
    this._suspicion = deps.suspicion;
    this._interaction = deps.interactionSystem;
    this._tts = deps.ttsClient;
    this._voice = deps.voiceClient;
    this._clueSystem = deps.clueSystem;
    this._playerModel = deps.playerModel;
    this._canvas = deps.canvas;
    this._collisionBoxes = deps.collisionBoxes || [];
    this._radius = CONFIG.PLAYER_RADIUS; // reuse same collision radius

    /** @type {'idle'|'approaching'|'checking_in'|'leaving'|'alert'} */
    this.state = 'idle';

    this._timer = 0;
    this._nextCheckinAt = AI().FIRST_CHECKIN_DELAY;
    this._stateTime = 0;
    this._started = false;

    // Saved home position for returning
    this._homePos = new THREE.Vector3(
      CONFIG.MILCHICK_HOME.x, 0, CONFIG.MILCHICK_HOME.z
    );
    this._moveTarget = new THREE.Vector3();
    this._moveStart = new THREE.Vector3();
    this._moveDistance = 0;       // total distance for current move
    this._moveProgress = 0;       // distance traveled so far

    // Prevent re-entrance of async _beginCheckin
    this._inCheckin = false;

    // Track whether we interrupted a clue interaction
    this._interruptedClue = false;
  }

  /** Start the check-in loop. Call after intro sequence completes. */
  start() {
    this._started = true;
    this._timer = 0;
    this._scheduleNextCheckin(AI().FIRST_CHECKIN_DELAY);
  }

  /** Stop the AI (e.g., on game over). */
  stop() {
    this._started = false;
  }

  /**
   * Call every frame from the game loop.
   * @param {number} dt
   */
  update(dt) {
    if (!this._started || this.state === 'alert') return;

    this._timer += dt;
    this._stateTime += dt;

    switch (this.state) {
      case 'idle':
        this._updateIdle(dt);
        break;
      case 'approaching':
        // Wait for active clue to finish before arriving
        if (this._suspicion.playerInClueInteraction) break;
        this._updateMoving(dt, () => this._beginCheckin(), true);
        break;
      case 'leaving':
        this._updateMoving(dt, () => this._arriveHome(), false);
        break;
      // checking_in is async, no frame update needed
    }
  }

  // ── State updates ─────────────────────────────────────────────

  _updateIdle() {
    if (this._timer >= this._nextCheckinAt) {
      // Don't interrupt an active clue session — defer the check-in
      if (this._suspicion.playerInClueInteraction) {
        this._nextCheckinAt = this._timer + 5; // retry in 5s
        return;
      }
      this._beginApproach();
    }
  }

  /**
   * Distance-based movement at constant walk speed.
   * Used for both approach and leaving states.
   * @param {boolean} trackPlayer  If true, continuously update target toward player
   */
  _updateMoving(dt, onArrive, trackPlayer = false) {
    // If tracking player, update the target each frame so Milchick walks
    // toward where the player IS, not where they were when the approach started
    if (trackPlayer) {
      const playerPos = this._playerModel.position;
      const milPos = this._milchick.model.position;
      const dir = new THREE.Vector3().subVectors(playerPos, milPos);
      dir.y = 0;
      const distToPlayer = dir.length();

      // Must walk for at least 1 second before arriving (prevents instant arrive stutter)
      const minApproachTime = 1.0;
      const canArrive = this._stateTime >= minApproachTime;

      // Stop when within approach distance of the player (and min time elapsed)
      if (distToPlayer <= AI().APPROACH_DISTANCE && canArrive) {
        onArrive();
        return;
      }

      // Move toward player at constant speed
      // (no collision — slide collision causes Milchick to get stuck on furniture)
      if (distToPlayer > AI().APPROACH_DISTANCE) {
        dir.normalize();
        const step = AI().WALK_SPEED * dt;
        milPos.x += dir.x * step;
        milPos.z += dir.z * step;
      }
      this._milchick.model.rotation.y = Math.atan2(dir.x, dir.z);
      return;
    }

    // Fixed-target movement (for leaving) — no collision so Milchick
    // can always reach home (he walks through furniture on the way back,
    // which is fine since the player sees him walking away)
    const pos = this._milchick.model.position;
    const dir = new THREE.Vector3().subVectors(this._moveTarget, pos);
    dir.y = 0;
    const dist = dir.length();

    if (dist < 0.1) {
      onArrive();
      return;
    }

    dir.normalize();
    const step = Math.min(AI().WALK_SPEED * dt, dist);
    pos.x += dir.x * step;
    pos.z += dir.z * step;
    this._milchick.model.rotation.y = Math.atan2(dir.x, dir.z);
  }

  // ── State transitions ─────────────────────────────────────────

  _beginApproach() {
    // Check if player is currently in a clue interaction
    this._interruptedClue = this._suspicion.playerInClueInteraction;

    // Warning cue
    this._ui.showSubtitle('', 'You hear footsteps approaching...', AI().WARNING_LEAD_TIME * 1000);

    this._milchick.playWalk();
    this._setState('approaching');
  }

  async _beginCheckin() {
    if (this._inCheckin || !this._started) return; // prevent double-fire or post-game

    // Safety: if player is still in a clue, go home instead
    if (this._suspicion.playerInClueInteraction) {
      this._moveTarget.copy(this._homePos);
      this._milchick.playWalk();
      this._setState('leaving');
      return;
    }

    this._inCheckin = true;
    this._setState('checking_in');
    this._milchick.startTalking();
    this._facePlayer();

    // Pause clue interaction
    const wasInteractionEnabled = this._interaction.enabled;
    this._interaction.enabled = false;

    const riskMod = this._suspicion.getRiskModifier();

    // Voice session speaks the greeting via assistant_speaks_first=true
    let classification = 'innocent';
    try {
      classification = await this._runVoiceCheckin(riskMod);
    } catch (e) {
      console.error('[MilchickAI] Voice check-in error:', e);
      classification = riskMod > 0 ? 'nervous' : 'innocent';
    }

    // Bail if game ended during voice session (e.g. win/game-over)
    if (!this._started) { this._inCheckin = false; return; }

    // Apply suspicion based on classification + context
    this._applySuspicion(classification, riskMod);

    // Milchick reacts
    await this._milchickReact(classification, riskMod);

    // Bail if game ended during reaction
    if (!this._started) { this._inCheckin = false; return; }

    // Check for caught state
    if (this._suspicion.level >= CONFIG.SUSPICION_MAX) {
      this._triggerAlert();
      return;
    }

    // Restore interaction and pointer lock
    this._interaction.enabled = wasInteractionEnabled;

    try {
      await this._canvas.requestPointerLock();
    } catch (e) {
      console.warn('[MilchickAI] Could not re-acquire pointer lock:', e);
    }

    // Begin leaving
    this._inCheckin = false;
    this._moveTarget.copy(this._homePos);
    this._milchick.playWalk();
    this._setState('leaving');
  }

  _arriveHome() {
    this._milchick.placeAt(this._homePos.x, this._homePos.z, Math.PI);
    this._milchick.setIdle();
    this._setState('idle');
    this._scheduleNextCheckin();
  }

  /**
   * Run a voice check-in session via VoiceClient (gradbot).
   * Uses the same AudioProcessor + PCM playback pipeline as clue sessions.
   * @param {number} riskMod
   * @returns {Promise<'innocent'|'nervous'|'suspicious'>}
   */
  async _runVoiceCheckin(riskMod) {
    // Show voice panel for player to respond
    const panelPromise = this._ui.showVoicePanel();

    try {
      // Connect to /ws/checkin via VoiceClient (uses gradbot AudioProcessor)
      await this._voice.connectCheckin();

      // Race: classification from backend vs panel close vs timeout
      return await Promise.race([
        this._voice.waitForCheckinResult(AI().VOICE_TIMEOUT).catch(() => {
          console.log('[MilchickAI] Check-in timeout (silence), classifying as nervous');
          return 'nervous';
        }),
        panelPromise.then(() => 'nervous'),
      ]);
    } catch (e) {
      console.error('[MilchickAI] Check-in session error:', e);
      return 'nervous';
    } finally {
      this._voice.disconnect();
      this._ui.hideVoicePanel();
    }
  }

  /**
   * Apply suspicion change based on voice classification + risk context.
   */
  _applySuspicion(classification, riskMod) {
    let delta = 0;

    switch (classification) {
      case 'innocent':
        // If no risk, suspicion may drop slightly
        if (riskMod === 0 && this._suspicion.level > 0) {
          this._suspicion.lower(1);
        }
        break;
      case 'nervous':
        delta = 1;
        break;
      case 'suspicious':
        delta = 2;
        break;
    }

    // Risk modifier adds to suspicion
    if (riskMod >= 2) delta += 1;

    if (delta > 0) {
      this._suspicion.raise(delta);
    }
  }

  /**
   * Milchick reacts to the check-in result with animation + subtitle.
   */
  async _milchickReact(classification, riskMod) {
    let line, animFn;

    if (classification === 'suspicious' || (classification === 'nervous' && riskMod >= 2)) {
      const lines = [
        "That's... not very convincing, Laurent.",
        "I'm going to have to note this.",
        "Laurent, you seem distracted from your work.",
      ];
      line = lines[Math.floor(Math.random() * lines.length)];
      animFn = () => this._milchick.playSuspicious();
    } else if (classification === 'nervous') {
      const lines = [
        "Hmm. Try to stay focused.",
        "Alright. Back to work then.",
        "Your department needs you, Laurent.",
      ];
      line = lines[Math.floor(Math.random() * lines.length)];
      animFn = () => this._milchick.playSuspicious();
    } else {
      const lines = [
        "Good. Keep up the excellent work.",
        "That's what I like to hear.",
        "Wonderful. Gradium appreciates your dedication.",
      ];
      line = lines[Math.floor(Math.random() * lines.length)];
      animFn = () => this._milchick.startTalking();
    }

    animFn();

    this._ui.showSubtitle('Neil', `"${line}"`, 0);
    let audioPlayed = false;

    if (this._tts) {
      try {
        await this._tts.speak(line, 'Jack', {
          onFirstAudio: () => { audioPlayed = true; },
        });
      } catch {
        // TTS failed, subtitle is already showing
      }
    }

    if (!audioPlayed) {
      await _wait(3000);
    }
    this._ui.hideSubtitle();
  }

  _triggerAlert() {
    this._setState('alert');
    this._milchick.playAngry();
    this.stop();
    // onCaught callback in SuspicionSystem handles the game over UI
  }

  // ── Helpers ───────────────────────────────────────────────────

  _setState(state) {
    this.state = state;
    this._stateTime = 0;
  }

  _scheduleNextCheckin(fixedDelay) {
    if (fixedDelay != null) {
      this._nextCheckinAt = this._timer + fixedDelay;
    } else {
      const interval = _randomRange(AI().CHECKIN_INTERVAL_MIN, AI().CHECKIN_INTERVAL_MAX);
      this._nextCheckinAt = this._timer + interval;
    }
  }

  _facePlayer() {
    const milPos = this._milchick.model.position;
    const playerPos = this._playerModel.position;
    const dir = new THREE.Vector3().subVectors(playerPos, milPos);
    dir.y = 0;
    if (dir.lengthSq() > 0.01) {
      this._milchick.model.rotation.y = Math.atan2(dir.x, dir.z);
    }
  }

  /** Slide-collision move: try X then Z independently, same as player controller. */
  _moveWithCollision(pos, dx, dz) {
    const r = this._radius;
    const newX = pos.x + dx;
    if (!this._collides(newX, pos.z, r)) pos.x = newX;
    const newZ = pos.z + dz;
    if (!this._collides(pos.x, newZ, r)) pos.z = newZ;
  }

  _collides(x, z, r) {
    for (const box of this._collisionBoxes) {
      const closestX = Math.max(box.min.x, Math.min(x, box.max.x));
      const closestZ = Math.max(box.min.z, Math.min(z, box.max.z));
      const dx = x - closestX;
      const dz = z - closestZ;
      if (dx * dx + dz * dz < r * r) return true;
    }
    return false;
  }
}
