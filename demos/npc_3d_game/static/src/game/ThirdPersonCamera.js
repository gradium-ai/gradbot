import * as THREE from 'three';
import { CONFIG } from '../config.js';

/**
 * Close over-the-shoulder camera that tracks a target object.
 *
 * The camera sits behind and slightly to the right of the target
 * at shoulder height, looking at a point just above the target's center.
 *
 * Mouse input rotates the camera orbit; the target character will
 * face the camera's forward direction when moving.
 */
export class ThirdPersonCamera {
  constructor(camera, domElement) {
    this.camera = camera;
    this.domElement = domElement;

    // Orbit angles (radians). yaw=0 places camera at +Z looking toward -Z
    this._yaw = 0;         // behind Mark who faces -Z (toward the desks)
    this._pitch = 0.08;    // near-level, shoulder height

    // Smoothed position for stable feel
    this._currentPos = new THREE.Vector3();
    this._currentLookAt = new THREE.Vector3();
    this._isFirstFrame = true;

    // Wall collision raycast
    this._raycaster = new THREE.Raycaster();
    this._raycaster.near = 0;
    /** @type {THREE.Mesh[]} Set via setWallMeshes() */
    this._wallMeshes = [];

    this._setupMouseLook();
  }

  /** Provide wall/ceiling/floor meshes for camera collision. */
  setWallMeshes(meshes) {
    this._wallMeshes = meshes;
  }

  _setupMouseLook() {
    document.addEventListener('mousemove', (e) => {
      if (!document.pointerLockElement) return;

      this._yaw   -= e.movementX * CONFIG.MOUSE_SENSITIVITY;
      this._pitch -= e.movementY * CONFIG.MOUSE_SENSITIVITY;

      // Clamp pitch so camera doesn't flip or go underground
      this._pitch = Math.max(-0.3, Math.min(0.8, this._pitch));
    });
  }

  /**
   * @param {THREE.Vector3} targetPos  World position of the character's feet
   * @param {number} dt  Delta time in seconds
   */
  update(targetPos, dt) {
    const cfg = CONFIG.CAMERA_3P;

    // Shoulder-height pivot point on the character
    const pivotY = targetPos.y + cfg.HEIGHT;

    // Compute ideal camera position from spherical offset
    const idealPos = new THREE.Vector3(
      targetPos.x + Math.sin(this._yaw) * cfg.DISTANCE + cfg.SHOULDER_OFFSET_X * Math.cos(this._yaw),
      pivotY + Math.sin(this._pitch) * cfg.DISTANCE,
      targetPos.z + Math.cos(this._yaw) * cfg.DISTANCE + cfg.SHOULDER_OFFSET_X * -Math.sin(this._yaw)
    );

    // Look-at target is slightly above the character pivot (chest level)
    const idealLookAt = new THREE.Vector3(
      targetPos.x,
      pivotY + cfg.LOOK_AT_OFFSET_Y,
      targetPos.z
    );

    // Pull camera forward if a wall is between player and ideal position
    if (this._wallMeshes.length > 0) {
      const dir = new THREE.Vector3().subVectors(idealPos, idealLookAt);
      const dist = dir.length();
      dir.normalize();
      this._raycaster.set(idealLookAt, dir);
      this._raycaster.far = dist;
      const hits = this._raycaster.intersectObjects(this._wallMeshes, false);
      if (hits.length > 0) {
        // Place camera slightly in front of the wall (0.2 offset to avoid z-fighting)
        const safeDist = Math.max(0.3, hits[0].distance - 0.2);
        idealPos.copy(idealLookAt).addScaledVector(dir, safeDist);
      }
    }

    if (this._isFirstFrame) {
      this._currentPos.copy(idealPos);
      this._currentLookAt.copy(idealLookAt);
      this._isFirstFrame = false;
    } else {
      // Smooth follow
      const t = 1 - Math.pow(1 - cfg.SMOOTHING, dt * 60);
      this._currentPos.lerp(idealPos, t);
      this._currentLookAt.lerp(idealLookAt, t);
    }

    this.camera.position.copy(this._currentPos);
    this.camera.lookAt(this._currentLookAt);
  }

  /** Forward direction on the XZ plane (for movement relative to camera). */
  getForward() {
    const forward = new THREE.Vector3(
      -Math.sin(this._yaw),
      0,
      -Math.cos(this._yaw)
    );
    return forward.normalize();
  }

  /** Right direction on the XZ plane. */
  getRight() {
    const forward = this.getForward();
    return new THREE.Vector3().crossVectors(forward, new THREE.Vector3(0, 1, 0)).normalize();
  }

  get yaw() { return this._yaw; }
}
