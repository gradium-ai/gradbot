import * as THREE from 'three';
import { CONFIG } from '../config.js';

/**
 * Hybrid interaction system: raycast + proximity.
 *
 * - Raycast interactables trigger when the screen-center ray hits them.
 * - Proximity interactables trigger when the player walks within radius.
 *   (Solves the third-person problem where the ray hits the player model
 *    before reaching floor-level objects.)
 *
 * Proximity takes priority — if the player is near a proximity object,
 * that shows even if the raycast is hitting something else.
 */
export class InteractionSystem {
  /**
   * @param {THREE.Camera} camera
   * @param {HTMLElement} domElement
   */
  constructor(camera, domElement) {
    this.camera = camera;
    this.domElement = domElement;

    this.enabled = true;

    /** @type {THREE.Vector3|null} Set by caller each frame */
    this._playerPos = new THREE.Vector3();

    this._raycaster = new THREE.Raycaster();
    this._raycaster.far = CONFIG.INTERACT_RANGE;
    this._screenCenter = new THREE.Vector2(0, 0);

    /** @type {Map<THREE.Object3D, InteractableData>} */
    this._interactables = new Map();

    /** @type {Array<{position: THREE.Vector3, radius: number, data: InteractableData}>} */
    this._proximityTriggers = [];

    /** @type {InteractableData|null} */
    this._hovered = null;

    this._buildUI();
    this._setupInput();
  }

  // ── Public API ──────────────────────────────────────────────

  /**
   * Register a raycast interactable.
   * @param {THREE.Object3D} object
   * @param {{name: string, onInteract: function}} data
   */
  register(object, { name, onInteract }) {
    this._interactables.set(object, { name, onInteract });
  }

  /**
   * Register a proximity interactable (triggers when player is nearby).
   * @param {THREE.Vector3} position  World position of the trigger center
   * @param {number} radius  Activation radius
   * @param {{name: string, onInteract: function}} data
   */
  registerProximity(position, radius, { name, onInteract }) {
    this._proximityTriggers.push({ position, radius, data: { name, onInteract } });
  }

  /**
   * Call every frame.
   * @param {THREE.Vector3} [playerPos]  Player world position (for proximity checks)
   */
  update(playerPos) {
    if (!this.enabled || !document.pointerLockElement) {
      this._setHovered(null);
      return;
    }

    if (playerPos) this._playerPos.copy(playerPos);

    // 1. Check proximity triggers first (they take priority)
    let proximityHit = null;
    let closestDist = Infinity;
    for (const trigger of this._proximityTriggers) {
      const dx = this._playerPos.x - trigger.position.x;
      const dz = this._playerPos.z - trigger.position.z;
      const dist = Math.sqrt(dx * dx + dz * dz);
      if (dist < trigger.radius && dist < closestDist) {
        closestDist = dist;
        proximityHit = trigger.data;
      }
    }

    if (proximityHit) {
      this._setHovered(proximityHit);
      return;
    }

    // 2. Fall back to raycast
    this._raycaster.setFromCamera(this._screenCenter, this.camera);

    const targets = [];
    for (const obj of this._interactables.keys()) {
      if (obj.isMesh) {
        targets.push(obj);
      } else {
        obj.traverse((child) => {
          if (child.isMesh) targets.push(child);
        });
      }
    }

    const hits = this._raycaster.intersectObjects(targets, false);

    if (hits.length > 0) {
      const hitMesh = hits[0].object;
      const data = this._findOwner(hitMesh);
      this._setHovered(data);
    } else {
      this._setHovered(null);
    }
  }

  dispose() {
    this._tooltip.remove();
    this._reticle.remove();
  }

  // ── Private ─────────────────────────────────────────────────

  _findOwner(mesh) {
    let current = mesh;
    while (current) {
      if (this._interactables.has(current)) {
        return this._interactables.get(current);
      }
      current = current.parent;
    }
    return null;
  }

  _setHovered(data) {
    if (data === this._hovered) return;
    this._hovered = data;

    if (data) {
      this._tooltip.textContent = `[E] ${data.name}`;
      this._tooltip.style.display = 'block';
      this._reticle.style.background = 'rgba(0, 204, 102, 0.9)';
      this._reticle.style.boxShadow = '0 0 6px rgba(0, 204, 102, 0.5)';
      this._reticle.style.width = '6px';
      this._reticle.style.height = '6px';
    } else {
      this._tooltip.style.display = 'none';
      this._reticle.style.background = 'rgba(255, 255, 255, 0.3)';
      this._reticle.style.boxShadow = 'none';
      this._reticle.style.width = '4px';
      this._reticle.style.height = '4px';
    }
  }

  _tryInteract() {
    if (!this.enabled || !document.pointerLockElement) return;
    if (this._hovered && this._hovered.onInteract) {
      this._hovered.onInteract();
    }
  }

  _setupInput() {
    window.addEventListener('keydown', (e) => {
      if (e.code === 'KeyE') this._tryInteract();
    });
    this.domElement.addEventListener('click', () => {
      this._tryInteract();
    });
  }

  _buildUI() {
    this._reticle = document.createElement('div');
    this._reticle.style.cssText = `
      position: fixed; top: 50%; left: 50%;
      transform: translate(-50%, -50%);
      width: 4px; height: 4px;
      background: rgba(255,255,255,0.3);
      border-radius: 50%;
      pointer-events: none;
      z-index: 50;
      transition: all 0.15s ease;
    `;
    document.body.appendChild(this._reticle);

    this._tooltip = document.createElement('div');
    this._tooltip.style.cssText = `
      position: fixed; top: calc(50% + 20px); left: 50%;
      transform: translateX(-50%);
      padding: 4px 12px;
      background: rgba(0, 0, 0, 0.7);
      color: #00cc66;
      font-family: 'Courier New', monospace;
      font-size: 13px;
      letter-spacing: 1px;
      border: 1px solid rgba(0, 204, 102, 0.3);
      pointer-events: none;
      z-index: 50;
      display: none;
      white-space: nowrap;
    `;
    document.body.appendChild(this._tooltip);
  }
}
