import * as THREE from 'three';
import { CharacterLoader } from './CharacterLoader.js';
import { NPCAnimator } from './NPCAnimator.js';
import { CONFIG } from '../config.js';

/**
 * GLB clip name → semantic name mapping for Neil.
 * Keys are the raw clip names from the GLB, values are the
 * internal names used by the game systems.
 */
const CLIP_NAME_MAP = {
  'Idle':            'idle',
  'Talking':         'talk',
  'Walking':         'walk',
  'Angry Gesture':   'angry',
  'Angry Point':     'gesture',
  'Looking Around':  'suspicious',
  'Silly Dancing':   'dance',
};

/**
 * Neil NPC — loads the combined GLB, places the model,
 * and exposes a high-level animation API for game systems.
 *
 * Usage:
 *   const neil = new Neil();
 *   const { model } = await neil.load();
 *   scene.add(model);
 *   // in loop:
 *   neil.update(dt);
 *   // trigger animations:
 *   neil.startTalking();
 *   neil.setIdle();
 */
export class Neil {
  constructor() {
    /** @type {THREE.Group|null} */
    this.model = null;
    /** @type {NPCAnimator|null} */
    this.animator = null;
    this._loader = new CharacterLoader();
  }

  /**
   * Load and prepare the Neil model.
   * @returns {Promise<{model: THREE.Group, animator: NPCAnimator}>}
   */
  async load() {
    const { model, mixer, animations } = await this._loader.load(
      'assets/glb/severance/neil.glb',
      {
        scale: CONFIG.NEIL_SCALE,
        castShadow: true,
        receiveShadow: true,
      }
    );

    this.model = model;

    // Build semantic clip map
    const clipMap = new Map();
    for (const [rawName, semanticName] of Object.entries(CLIP_NAME_MAP)) {
      const clip = animations.get(rawName);
      if (clip) {
        clipMap.set(semanticName, clip);
      } else {
        console.warn(`[Neil] Missing animation clip: "${rawName}" → "${semanticName}"`);
      }
    }

    this.animator = new NPCAnimator(mixer, clipMap);

    // Default to idle
    if (this.animator.has('idle')) {
      this.animator.play('idle');
    }

    return { model, animator: this.animator };
  }

  /**
   * Place Neil at a world position, facing a direction.
   * @param {number} x
   * @param {number} z
   * @param {number} [facingY=0]  Rotation around Y axis
   */
  placeAt(x, z, facingY = 0) {
    if (!this.model) return;
    this.model.position.set(x, 0, z);
    this.model.rotation.y = facingY;
  }

  /**
   * Call every frame.
   * @param {number} dt
   */
  update(dt) {
    if (this.animator) {
      this.animator.update(dt);
    }
  }

  // ── High-level animation triggers ──────────────────────────
  // All methods guard against missing clips via NPCAnimator's has() check

  _safePlay(name, duration, opts) {
    if (!this.animator?.has(name)) {
      console.warn(`[Neil] Cannot play "${name}" — clip not loaded`);
      return;
    }
    this.animator.crossFadeTo(name, duration, opts);
  }

  setIdle() { this._safePlay('idle', 0.4); }
  startTalking() { this._safePlay('talk', 0.3); }
  stopTalking() { this.setIdle(); }
  playWalk() { this._safePlay('walk', 0.3); }
  playAngry() { this._safePlay('angry', 0.3); }
  playSuspicious() { this._safePlay('suspicious', 0.4); }
  playGesture() { this._safePlay('gesture', 0.3, { loop: false }); }
  playDance() { this._safePlay('dance', 0.4); }
}
