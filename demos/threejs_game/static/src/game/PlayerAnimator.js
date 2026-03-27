import * as THREE from 'three';
import { CONFIG } from '../config.js';

/**
 * Manages all of Mark's animation states.
 *
 * Core movement uses weight-blending: idle weight = 1 - speedFactor,
 * active locomotion weight = speedFactor.
 *
 * Walking and sprinting share the same walk clip (sprint just plays faster).
 * Sneaking uses its own clip.
 */

const CLIP_MAP = {
  'Sitting Idle':     'seated_idle',
  'Typing':           'typing',
  'Sit To Stand':     'sit_to_stand',
  'Idle':             'idle',
  'Walking':          'walking',
  'Sneaking Forward': 'sneaking',
  'Look Around':      'look_around',
  'Swing Dancing':    'dancing',
};

// Only two locomotion clips: walking (also used for sprint) and sneaking
const LOCO_NAMES = ['walking', 'sneaking'];

export class PlayerAnimator {
  constructor(mixer, animations) {
    this._mixer = mixer;
    this._clips = new Map();
    this._actions = new Map();
    this._state = 'idle';
    this._lookAroundPlayed = false;
    this._activeLocoMode = 'walking';
    this._locoBlend = { from: null, to: 'walking', t: 1 };

    // Typing SFX — loops while Mark is seated typing
    this._typingSfx = new Audio('assets/audio/typing.mp3');
    this._typingSfx.loop = true;
    this._typingSfx.volume = 0.3;

    // Grab the idle clip's first-frame Hips position as the reference.
    // The Armature has a 90° X rotation so bone Z is actually world height.
    const idleClip = animations.get('Idle');
    this._hipsRestPos = [0, 0, 0];
    if (idleClip) {
      for (const t of idleClip.tracks) {
        if ((/hips/i.test(t.name) || /root/i.test(t.name)) && t.name.endsWith('.position')) {
          this._hipsRestPos = [t.values[0], t.values[1], t.values[2]];
          console.log('[PlayerAnimator] Hips rest pos (from idle):', this._hipsRestPos, 'track:', t.name);
          break;
        }
      }
    }

    for (const [glbName, semanticName] of Object.entries(CLIP_MAP)) {
      let clip = animations.get(glbName);
      if (!clip) continue;

      // Strip root motion from gameplay clips only — intro clips (seated, typing,
      // sit_to_stand) need their own Hips position for correct chair height
      const GAMEPLAY_CLIPS = ['idle', 'walking', 'sneaking', 'look_around', 'dancing'];
      if (GAMEPLAY_CLIPS.includes(semanticName)) {
        clip = this._stripRootMotion(clip);
      }

      this._clips.set(semanticName, clip);
      const action = mixer.clipAction(clip);
      action.setEffectiveWeight(0);
      this._actions.set(semanticName, action);
    }

    // Start idle and loco actions (weight 0) so they blend smoothly
    const idleAction = this._actions.get('idle');
    if (idleAction) { idleAction.play(); idleAction.setEffectiveWeight(1); }

    for (const name of LOCO_NAMES) {
      const a = this._actions.get(name);
      if (a) { a.play(); a.setEffectiveWeight(0); }
    }

    mixer.addEventListener('finished', (e) => {
      if (e.action === this._actions.get('sit_to_stand')) {
        this._onSitToStandFinished?.();
      }
      if (e.action === this._actions.get('look_around') && this._state === 'look_around') {
        this._enterLocomotion();
      }
    });
  }

  _stripRootMotion(clip) {
    clip = clip.clone();
    clip.tracks = clip.tracks.map(t => {
      const isRoot = /hips/i.test(t.name) || /root/i.test(t.name);
      const isPosition = t.name.endsWith('.position');
      if (isRoot && isPosition) {
        const values = t.values.slice();
        // Pin all Hips position components to idle rest pose so clips
        // blend without any drift (bone Z is world height due to Armature rotation)
        const [rx, ry, rz] = this._hipsRestPos;
        for (let i = 0; i < values.length; i += 3) {
          values[i] = rx;
          values[i + 1] = ry;
          values[i + 2] = rz;
        }
        return new THREE.VectorKeyframeTrack(t.name, Array.from(t.times), Array.from(values));
      }
      return t;
    });
    return clip;
  }

  // ── Intro ─────────────────────────────────────────────────────

  playSeated() {
    this._stopAll();
    const seated = this._actions.get('seated_idle');
    const typing = this._actions.get('typing');
    if (seated) {
      seated.setLoop(THREE.LoopRepeat);
      seated.setEffectiveWeight(0.4);
      seated.play();
    }
    if (typing) {
      typing.setLoop(THREE.LoopRepeat);
      typing.setEffectiveWeight(1);
      typing.play();
    }
    this._state = 'typing';
    this._typingSfx.currentTime = 0;
    this._typingSfx.play().catch(() => {});
  }

  playSitToStand() {
    return new Promise((resolve) => {
      this._onSitToStandFinished = resolve;
      const action = this._actions.get('sit_to_stand');
      if (!action) { resolve(); return; }

      const seated = this._actions.get('seated_idle');
      const typing = this._actions.get('typing');
      if (seated) seated.fadeOut(0.3);
      if (typing) typing.fadeOut(0.3);

      // Fade out typing SFX
      this._fadeOutTypingSfx();

      action.reset();
      action.setLoop(THREE.LoopOnce);
      action.clampWhenFinished = true;
      action.setEffectiveWeight(1);
      action.fadeIn(0.3).play();
      this._state = 'sit_to_stand';
    });
  }

  transitionToGameplay() {
    const sts = this._actions.get('sit_to_stand');
    if (sts) sts.fadeOut(0.3);
    this._enterLocomotion();
  }

  // ── Dance ─────────────────────────────────────────────────────

  playDance() {
    for (const name of ['idle', ...LOCO_NAMES]) {
      const a = this._actions.get(name);
      if (a) a.fadeOut(0.8);
    }
    const dance = this._actions.get('dancing');
    if (dance) {
      dance.reset();
      dance.setLoop(THREE.LoopRepeat);
      dance.setEffectiveWeight(1);
      dance.fadeIn(0.8).play();
    }
    this._state = 'dancing';
  }

  // ── Gameplay update ───────────────────────────────────────────

  update(dt, state) {
    if (['typing', 'sit_to_stand', 'dancing'].includes(this._state)) return;

    const { movementState, speedFactor, nearClue, idleTime } = state;

    if (movementState !== 'idle') {
      this._lookAroundPlayed = false;
    }

    // Look-around (one-shot)
    if (this._state === 'look_around') {
      if (movementState !== 'idle') {
        const la = this._actions.get('look_around');
        if (la) { la.stop(); la.setEffectiveWeight(0); }
        this._enterLocomotion();
      }
      return;
    }

    // Look-around disabled — the animation's root motion sinks Mark into the ground

    // ── Weight-blending locomotion ──────────────────────────────
    // Sprint uses the walking clip (just played faster), so map sprinting → walking
    const targetLocoClip = movementState === 'sneaking' ? 'sneaking' : 'walking';

    // Smooth transitions between walk and sneak clips
    const blend = this._locoBlend;
    if (targetLocoClip !== blend.to) {
      blend.from = blend.to;
      blend.to = targetLocoClip;
      blend.t = 0;
    }
    if (blend.t < 1) {
      blend.t = Math.min(1, blend.t + dt / 0.3);
    }

    const idleAction = this._actions.get('idle');

    // Determine movement speed for animation scaling
    const moveSpeed = movementState === 'sprinting' ? CONFIG.SPRINT_SPEED
                    : movementState === 'sneaking'  ? CONFIG.SNEAK_SPEED
                    : CONFIG.PLAYER_SPEED;

    for (const name of LOCO_NAMES) {
      const action = this._actions.get(name);
      if (!action) continue;

      let locoWeight = 0;
      if (name === blend.to) {
        locoWeight = blend.t;
      } else if (name === blend.from && blend.t < 1) {
        locoWeight = 1 - blend.t;
      }

      action.setEffectiveWeight(locoWeight * speedFactor);

      // Scale animation speed: walk clip plays faster for sprint
      const animSpeed = name === 'sneaking' ? CONFIG.SNEAK_ANIM_SPEED
                      : CONFIG.WALK_ANIM_SPEED;
      action.setEffectiveTimeScale(
        speedFactor > 0.01 ? (moveSpeed * speedFactor) / animSpeed : 1
      );
    }

    if (idleAction) {
      idleAction.setEffectiveWeight(1 - speedFactor);
    }

    this._activeLocoMode = targetLocoClip;
    this._state = 'locomotion';
  }

  // ── Helpers ───────────────────────────────────────────────────

  _enterLocomotion() {
    const la = this._actions.get('look_around');
    if (la) { la.stop(); la.setEffectiveWeight(0); }

    const idleAction = this._actions.get('idle');
    if (idleAction) {
      idleAction.setEffectiveWeight(1);
      if (!idleAction.isRunning()) idleAction.play();
    }
    for (const name of LOCO_NAMES) {
      const a = this._actions.get(name);
      if (a) {
        a.setEffectiveWeight(0);
        if (!a.isRunning()) a.play();
      }
    }
    this._locoBlend = { from: null, to: 'walking', t: 1 };
    this._state = 'locomotion';
  }

  _stopAll() {
    for (const action of this._actions.values()) {
      action.stop();
      action.setEffectiveWeight(0);
    }
    this._fadeOutTypingSfx();
  }

  _fadeOutTypingSfx() {
    const sfx = this._typingSfx;
    if (sfx.paused) return;
    const start = sfx.volume;
    const startTime = performance.now();
    const step = () => {
      const t = Math.min((performance.now() - startTime) / 500, 1);
      sfx.volume = start * (1 - t);
      if (t < 1) requestAnimationFrame(step);
      else sfx.pause();
    };
    requestAnimationFrame(step);
  }
}
