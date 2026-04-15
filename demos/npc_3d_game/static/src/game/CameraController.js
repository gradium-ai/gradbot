import * as THREE from 'three';
import { CONFIG } from '../config.js';

const ISO_DIR = new THREE.Vector3(20, 20, 20).normalize();

export class CameraController {
  constructor(camera, target) {
    this.camera = camera;
    this.target = target;
  }

  snapTo(pos) {
    const dist = 40;
    this.camera.position.set(
      pos.x + ISO_DIR.x * dist,
      pos.y + ISO_DIR.y * dist,
      pos.z + ISO_DIR.z * dist
    );
    this.camera.lookAt(pos.x, pos.y, pos.z);
  }

  update(dt) {
    const pos = this.target.position;
    const dist = 40;
    const goalX = pos.x + ISO_DIR.x * dist;
    const goalY = pos.y + ISO_DIR.y * dist;
    const goalZ = pos.z + ISO_DIR.z * dist;

    const factor = 1 - Math.exp(-CONFIG.CAM_SMOOTHING * dt);

    this.camera.position.x += (goalX - this.camera.position.x) * factor;
    this.camera.position.y += (goalY - this.camera.position.y) * factor;
    this.camera.position.z += (goalZ - this.camera.position.z) * factor;

    this.camera.lookAt(pos.x, pos.y, pos.z);
  }
}
