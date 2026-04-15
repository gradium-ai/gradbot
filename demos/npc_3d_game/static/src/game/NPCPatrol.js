import * as THREE from 'three';
import { CONFIG } from '../config.js';
import { Materials } from '../world/Materials.js';

export class NPCPatrol {
  constructor(scene, office) {
    this.scene = scene;

    this.mesh = new THREE.Group();

    // Legs (dark pants)
    const legGeo = new THREE.BoxGeometry(0.18, 0.4, 0.18);
    const legL = new THREE.Mesh(legGeo, Materials.neilPants);
    legL.position.set(-0.12, 0.2, 0);
    legL.castShadow = true;
    this.mesh.add(legL);
    const legR = new THREE.Mesh(legGeo, Materials.neilPants);
    legR.position.set(0.12, 0.2, 0);
    legR.castShadow = true;
    this.mesh.add(legR);

    // Torso (tan suit)
    const torso = new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.55, 0.28),
      Materials.neilSuit
    );
    torso.position.y = 0.68;
    torso.castShadow = true;
    this.mesh.add(torso);

    // Arms
    const armGeo = new THREE.BoxGeometry(0.14, 0.45, 0.14);
    const armL = new THREE.Mesh(armGeo, Materials.neilSuit);
    armL.position.set(-0.32, 0.62, 0);
    armL.castShadow = true;
    this.mesh.add(armL);
    const armR = new THREE.Mesh(armGeo, Materials.neilSuit);
    armR.position.set(0.32, 0.62, 0);
    armR.castShadow = true;
    this.mesh.add(armR);

    // Head (skin)
    const head = new THREE.Mesh(
      new THREE.BoxGeometry(0.3, 0.3, 0.3),
      Materials.skin
    );
    head.position.y = 1.15;
    head.castShadow = true;
    this.mesh.add(head);

    // Hair (dark)
    const hair = new THREE.Mesh(
      new THREE.BoxGeometry(0.32, 0.1, 0.32),
      new THREE.MeshStandardMaterial({ color: 0x2a1a0a })
    );
    hair.position.y = 1.35;
    this.mesh.add(hair);

    scene.add(this.mesh);

    // Waypoints
    const halfD = CONFIG.ROOM_DEPTH / 2;
    const halfW = CONFIG.ROOM_WIDTH / 2;
    const T = CONFIG.WALL_THICKNESS;
    const hzLen = CONFIG.HALLWAY_Z_LENGTH;
    const hxLen = CONFIG.HALLWAY_X_LENGTH;

    this.waypoints = [
      new THREE.Vector3(0, 0, -halfD - T - 1),
      new THREE.Vector3(0, 0, -halfD - T - hzLen + 1),
      new THREE.Vector3(0, 0, -halfD - T - 1),
      new THREE.Vector3(0, 0, 0),
      new THREE.Vector3(halfW + T + 1, 0, 0),
      new THREE.Vector3(halfW + T + hxLen - 1, 0, 0),
      new THREE.Vector3(halfW + T + 1, 0, 0),
      new THREE.Vector3(0, 0, 0),
    ];

    this._waypointIndex = 0;
    this._forward = new THREE.Vector3(0, 0, -1);

    this.reset();
  }

  reset() {
    this._waypointIndex = 0;
    this.mesh.position.copy(this.waypoints[0]);
  }

  update(dt) {
    const target = this.waypoints[this._waypointIndex];
    const pos = this.mesh.position;
    const dir = new THREE.Vector3().subVectors(target, pos);
    dir.y = 0;
    const dist = dir.length();

    if (dist < 0.2) {
      this._waypointIndex = (this._waypointIndex + 1) % this.waypoints.length;
      return;
    }

    dir.normalize();
    this._forward.copy(dir);
    this.mesh.rotation.y = Math.atan2(dir.x, dir.z);

    const step = CONFIG.NEIL_SPEED * dt;
    if (step >= dist) {
      pos.copy(target);
    } else {
      pos.addScaledVector(dir, step);
    }
  }

  detectsPlayer(playerPos) {
    const toPlayer = new THREE.Vector3().subVectors(playerPos, this.mesh.position);
    toPlayer.y = 0;
    const dist = toPlayer.length();
    if (dist > CONFIG.NEIL_DETECT_RANGE) return false;

    toPlayer.normalize();
    const dot = this._forward.dot(toPlayer);
    return dot > CONFIG.NEIL_DETECT_DOT;
  }
}
