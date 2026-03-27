import * as THREE from 'three';

/**
 * Manages animation state for an NPC character.
 *
 * Wraps AnimationMixer with a semantic name layer and crossfade API.
 * Clips are registered with internal names (idle, talk, walk, etc.)
 * regardless of the raw clip names in the GLB.
 *
 * Usage:
 *   const animator = new NPCAnimator(mixer, clipMap);
 *   animator.play('idle');
 *   animator.crossFadeTo('talk', 0.4);
 */
export class NPCAnimator {
  /**
   * @param {THREE.AnimationMixer} mixer
   * @param {Map<string, THREE.AnimationClip>} clipMap  semantic-name → clip
   */
  constructor(mixer, clipMap) {
    this.mixer = mixer;

    /** @type {Map<string, THREE.AnimationAction>} */
    this._actions = new Map();

    /** @type {THREE.AnimationAction|null} */
    this._current = null;
    this._currentName = '';

    // Pre-create actions but do NOT play them — only the active one gets played
    for (const [name, clip] of clipMap) {
      const action = mixer.clipAction(clip);
      action.setEffectiveWeight(0);
      action.enabled = true;
      this._actions.set(name, action);
    }
  }

  /** Names of all registered clips. */
  get clipNames() { return [...this._actions.keys()]; }

  /** Currently playing clip name. */
  get currentClip() { return this._currentName; }

  /**
   * Instantly switch to a clip (no crossfade).
   * @param {string} name  Semantic clip name
   * @param {object} [opts]
   * @param {boolean} [opts.loop=true]
   */
  play(name, { loop = true } = {}) {
    const action = this._actions.get(name);
    if (!action) {
      console.warn(`[NPCAnimator] Clip "${name}" not found`);
      return;
    }

    // Stop all other actions cleanly
    this._stopAllExcept(action);

    action.setLoop(loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
    action.clampWhenFinished = !loop;
    action.setEffectiveWeight(1);
    action.setEffectiveTimeScale(1);
    action.reset();
    action.play();
    this._current = action;
    this._currentName = name;
  }

  /**
   * Crossfade from current clip to another.
   * @param {string} name      Semantic clip name
   * @param {number} [duration=0.4]  Crossfade duration in seconds
   * @param {object} [opts]
   * @param {boolean} [opts.loop=true]
   */
  crossFadeTo(name, duration = 0.4, { loop = true } = {}) {
    if (name === this._currentName) return;

    const next = this._actions.get(name);
    if (!next) {
      console.warn(`[NPCAnimator] Clip "${name}" not found`);
      return;
    }

    // Stop any actions that aren't current (cleanup leftover crossfades)
    this._stopAllExcept(this._current, next);

    next.setLoop(loop ? THREE.LoopRepeat : THREE.LoopOnce, Infinity);
    next.clampWhenFinished = !loop;
    next.reset();
    next.setEffectiveWeight(1);
    next.setEffectiveTimeScale(1);
    next.play();

    if (this._current) {
      this._current.crossFadeTo(next, duration, true);
    }

    this._current = next;
    this._currentName = name;
  }

  /**
   * Update the mixer. Call once per frame.
   * @param {number} dt  Delta time in seconds
   */
  update(dt) {
    this.mixer.update(dt);
  }

  /** Check if a clip name is registered. */
  has(name) { return this._actions.has(name); }

  /** Stop all actions except the ones specified. */
  _stopAllExcept(...keep) {
    for (const [, action] of this._actions) {
      if (!keep.includes(action)) {
        action.setEffectiveWeight(0);
        action.stop();
      }
    }
  }
}
