import * as THREE from 'three';
import { CONFIG } from '../config.js';

export class FirstPersonController {
  constructor(camera, domElement) {
    this.camera = camera;
    this.domElement = domElement;
    this.isLocked = false;

    // Movement state
    this._keys = {};
    this._euler = new THREE.Euler(0, 0, 0, 'YXZ');
    this._velocity = new THREE.Vector3();
    this._direction = new THREE.Vector3();

    // Position the camera
    this.camera.position.set(0, CONFIG.PLAYER_EYE_HEIGHT, 4);

    this._setupPointerLock();
    this._setupKeyboard();
  }

  _setupPointerLock() {
    this.domElement.addEventListener('click', () => {
      if (!this.isLocked) {
        this.domElement.requestPointerLock();
      }
    });

    document.addEventListener('pointerlockchange', () => {
      this.isLocked = document.pointerLockElement === this.domElement;
    });

    document.addEventListener('mousemove', (e) => {
      if (!this.isLocked) return;

      this._euler.setFromQuaternion(this.camera.quaternion);
      this._euler.y -= e.movementX * CONFIG.MOUSE_SENSITIVITY;
      this._euler.x -= e.movementY * CONFIG.MOUSE_SENSITIVITY;

      // Clamp vertical look
      this._euler.x = Math.max(-Math.PI / 2.5, Math.min(Math.PI / 2.5, this._euler.x));

      this.camera.quaternion.setFromEuler(this._euler);
    });
  }

  _setupKeyboard() {
    window.addEventListener('keydown', (e) => { this._keys[e.code] = true; });
    window.addEventListener('keyup', (e) => { this._keys[e.code] = false; });
  }

  update(dt, collisionBoxes) {
    if (!this.isLocked) return;

    const speed = CONFIG.PLAYER_SPEED;
    const pos = this.camera.position;
    const r = CONFIG.PLAYER_RADIUS;

    // Get forward/right vectors (flattened to XZ plane)
    const forward = new THREE.Vector3();
    this.camera.getWorldDirection(forward);
    forward.y = 0;
    forward.normalize();

    const right = new THREE.Vector3();
    right.crossVectors(forward, new THREE.Vector3(0, 1, 0)).normalize();

    // Compute movement direction from keys
    this._direction.set(0, 0, 0);
    if (this._keys['KeyW'] || this._keys['ArrowUp']) this._direction.add(forward);
    if (this._keys['KeyS'] || this._keys['ArrowDown']) this._direction.sub(forward);
    if (this._keys['KeyD'] || this._keys['ArrowRight']) this._direction.add(right);
    if (this._keys['KeyA'] || this._keys['ArrowLeft']) this._direction.sub(right);

    if (this._direction.lengthSq() === 0) return;
    this._direction.normalize();

    const moveX = this._direction.x * speed * dt;
    const moveZ = this._direction.z * speed * dt;

    // Slide collision: try X, then Z independently
    const newX = pos.x + moveX;
    if (!this._collides(newX, pos.z, r, collisionBoxes)) {
      pos.x = newX;
    }

    const newZ = pos.z + moveZ;
    if (!this._collides(pos.x, newZ, r, collisionBoxes)) {
      pos.z = newZ;
    }

    // Lock Y to eye height
    pos.y = CONFIG.PLAYER_EYE_HEIGHT;
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
    return this.camera.position;
  }
}
