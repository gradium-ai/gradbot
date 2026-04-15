import * as THREE from 'three';
import { CONFIG } from '../config.js';
import { Materials } from '../world/Materials.js';

export class Player {
  constructor(scene) {
    this.mesh = new THREE.Group();

    // Legs (dark pants)
    const legGeo = new THREE.BoxGeometry(0.18, 0.4, 0.18);
    const legL = new THREE.Mesh(legGeo, Materials.playerPants);
    legL.position.set(-0.12, 0.2, 0);
    legL.castShadow = true;
    this.mesh.add(legL);
    const legR = new THREE.Mesh(legGeo, Materials.playerPants);
    legR.position.set(0.12, 0.2, 0);
    legR.castShadow = true;
    this.mesh.add(legR);

    // Torso (Lumon blue shirt)
    const torso = new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.55, 0.28),
      Materials.playerShirt
    );
    torso.position.y = 0.68;
    torso.castShadow = true;
    this.mesh.add(torso);

    // Arms
    const armGeo = new THREE.BoxGeometry(0.14, 0.45, 0.14);
    const armL = new THREE.Mesh(armGeo, Materials.playerShirt);
    armL.position.set(-0.32, 0.62, 0);
    armL.castShadow = true;
    this.mesh.add(armL);
    const armR = new THREE.Mesh(armGeo, Materials.playerShirt);
    armR.position.set(0.32, 0.62, 0);
    armR.castShadow = true;
    this.mesh.add(armR);

    // Head (skin tone)
    const head = new THREE.Mesh(
      new THREE.BoxGeometry(0.3, 0.3, 0.3),
      Materials.skin
    );
    head.position.y = 1.15;
    head.castShadow = true;
    this.mesh.add(head);

    // Hair
    const hair = new THREE.Mesh(
      new THREE.BoxGeometry(0.32, 0.1, 0.32),
      Materials.playerPants
    );
    hair.position.y = 1.35;
    this.mesh.add(hair);

    this.mesh.position.set(0, 0, 3);
    scene.add(this.mesh);
  }

  update(dt, direction, collisionBoxes) {
    if (direction.x === 0 && direction.z === 0) return;

    const speed = CONFIG.PLAYER_SPEED;
    const r = CONFIG.PLAYER_RADIUS;
    const pos = this.mesh.position;

    this.mesh.rotation.y = Math.atan2(direction.x, direction.z);

    const newX = pos.x + direction.x * speed * dt;
    if (!this._collides(newX, pos.z, r, collisionBoxes)) {
      pos.x = newX;
    }

    const newZ = pos.z + direction.z * speed * dt;
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
}
