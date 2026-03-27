import * as THREE from 'three';
import { CONFIG } from '../config.js';

/**
 * Controls a character model from third-person perspective.
 *
 * Responsibilities:
 *  - Read keyboard input
 *  - Move the character model with collision
 *  - Smooth acceleration/deceleration to prevent sliding
 *  - Rotate the model to face movement direction
 *  - Expose state so animation system can pick the right clip later
 *
 * Does NOT own the camera — that's ThirdPersonCamera's job.
 */
export class ThirdPersonController {
  /**
   * @param {THREE.Group} model  The loaded character model (root group)
   * @param {import('./ThirdPersonCamera.js').ThirdPersonCamera} cameraController
   * @param {HTMLElement} domElement
   */
  constructor(model, cameraController, domElement) {
    this.model = model;
    this.cameraController = cameraController;
    this.domElement = domElement;

    // State
    this.enabled = true;
    this._keys = {};
    this._direction = new THREE.Vector3();
    this._targetYaw = 0;
    this._isMoving = false;

    // Velocity smoothing — 0 to 1 representing current speed factor
    this._speedFactor = 0;

    // Sneak / sprint / idle tracking
    this._isSneaking = false;
    this._isSprinting = false;
    this._idleTimer = 0;
    this._nearClue = false;
    /** @type {'idle'|'walking'|'sneaking'|'sprinting'} */
    this._movementState = 'idle';

    this._setupInput();
  }

  _setupInput() {
    window.addEventListener('keydown', (e) => { this._keys[e.code] = true; });
    window.addEventListener('keyup', (e) => { this._keys[e.code] = false; });
    // Clear all keys when window loses focus (prevents stuck keys on tab-away)
    window.addEventListener('blur', () => { this._keys = {}; });
    document.addEventListener('pointerlockchange', () => {
      if (!document.pointerLockElement) this._keys = {};
    });
  }

  /** True when the character is actively moving this frame. */
  get isMoving() { return this._isMoving; }

  /** Current speed factor (0–1) for animation blending. */
  get speedFactor() { return this._speedFactor; }

  get isSneaking() { return this._isSneaking; }
  get isSprinting() { return this._isSprinting; }
  get idleTime() { return this._idleTimer; }
  get movementState() { return this._movementState; }

  /** Set from game loop each frame — whether player is near a clue. */
  set nearClue(val) { this._nearClue = val; }

  /**
   * @param {number} dt  Delta time in seconds
   * @param {THREE.Box3[]} collisionBoxes  AABB boxes for slide-collision
   */
  update(dt, collisionBoxes) {
    if (!this.enabled || !document.pointerLockElement) {
      this._isMoving = false;
      this._isSneaking = false;
      this._isSprinting = false;
      this._movementState = 'idle';
      // Decelerate when disabled
      this._speedFactor = Math.max(0, this._speedFactor - CONFIG.PLAYER_DECEL * dt);
      return;
    }

    const pos = this.model.position;
    const r = CONFIG.PLAYER_RADIUS;

    // Camera-relative directions
    const forward = this.cameraController.getForward();
    const right = this.cameraController.getRight();

    // Accumulate input
    this._direction.set(0, 0, 0);
    if (this._keys['KeyW'] || this._keys['ArrowUp'])    this._direction.add(forward);
    if (this._keys['KeyS'] || this._keys['ArrowDown'])  this._direction.sub(forward);
    if (this._keys['KeyD'] || this._keys['ArrowRight']) this._direction.add(right);
    if (this._keys['KeyA'] || this._keys['ArrowLeft'])  this._direction.sub(right);

    const wantsToMove = this._direction.lengthSq() > 0;
    const shiftHeld = this._keys['ShiftLeft'] || this._keys['ShiftRight'];
    const sneakHeld = this._keys['KeyC'] || this._keys['ControlLeft'] || this._keys['ControlRight'];

    // Determine movement mode (sneak takes priority over sprint)
    this._isSneaking = wantsToMove && sneakHeld;
    this._isSprinting = wantsToMove && shiftHeld && !this._isSneaking;

    const maxSpeed = this._isSprinting ? CONFIG.SPRINT_SPEED
                   : this._isSneaking  ? CONFIG.SNEAK_SPEED
                   : CONFIG.PLAYER_SPEED;

    // Accelerate / decelerate speed factor
    if (wantsToMove) {
      this._speedFactor = Math.min(1, this._speedFactor + CONFIG.PLAYER_ACCEL * dt);
    } else {
      this._speedFactor = Math.max(0, this._speedFactor - CONFIG.PLAYER_DECEL * dt);
    }

    this._isMoving = this._speedFactor > 0.01;

    // Idle timer
    if (!this._isMoving) {
      this._idleTimer += dt;
      this._speedFactor = 0;
      this._movementState = 'idle';
      return;
    } else {
      this._idleTimer = 0;
    }

    // Movement state
    this._movementState = this._isSneaking ? 'sneaking'
                        : this._isSprinting ? 'sprinting'
                        : 'walking';

    if (wantsToMove) {
      this._direction.normalize();

      // Rotate model to face movement direction (smooth turn)
      this._targetYaw = Math.atan2(this._direction.x, this._direction.z);
    }

    const currentYaw = this.model.rotation.y;
    let diff = this._targetYaw - currentYaw;
    // Wrap to [-PI, PI]
    while (diff > Math.PI) diff -= Math.PI * 2;
    while (diff < -Math.PI) diff += Math.PI * 2;
    this.model.rotation.y += diff * Math.min(1, CONFIG.CHAR_TURN_SPEED * dt);

    // Movement with speed ramp
    const speed = maxSpeed * this._speedFactor;
    const moveX = this._direction.x * speed * dt;
    const moveZ = this._direction.z * speed * dt;

    // Slide collision (try each axis independently)
    const newX = pos.x + moveX;
    if (!this._collides(newX, pos.z, r, collisionBoxes)) {
      pos.x = newX;
    }

    const newZ = pos.z + moveZ;
    if (!this._collides(pos.x, newZ, r, collisionBoxes)) {
      pos.z = newZ;
    }
  }

  _collides(x, z, r, boxes) {
    for (const box of boxes) {
      const closestX = Math.max(box.min.x, Math.min(x, box.max.x));
      const closestZ = Math.max(box.min.z, Math.min(z, box.max.z));
      const dx = x - closestX;
      const dz = z - closestZ;
      if (dx * dx + dz * dz < r * r) return true;
    }
    return false;
  }

  getPosition() {
    return this.model.position;
  }
}
