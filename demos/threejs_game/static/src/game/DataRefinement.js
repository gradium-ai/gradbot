import * as THREE from 'three';
import { CONFIG } from '../config.js';

export class DataRefinement {
  constructor(scene) {
    this.scene = scene;
    this.stations = []; // { position, progress, complete, ring, progressRing }
  }

  addStation(x, z) {
    const position = new THREE.Vector3(x, 0, z);

    // Inactive ring on the floor around desk cluster
    const ringGeo = new THREE.RingGeometry(1.8, 2.0, 32);
    ringGeo.rotateX(-Math.PI / 2);
    const ringMat = new THREE.MeshBasicMaterial({
      color: 0x00cc66,
      transparent: true,
      opacity: 0.3,
      side: THREE.DoubleSide,
    });
    const ring = new THREE.Mesh(ringGeo, ringMat);
    ring.position.set(x, 0.02, z);
    this.scene.add(ring);

    // Progress ring (fills up as you refine)
    const progressGeo = new THREE.RingGeometry(2.0, 2.2, 32, 1, 0, 0);
    progressGeo.rotateX(-Math.PI / 2);
    const progressMat = new THREE.MeshBasicMaterial({
      color: 0x00ff88,
      transparent: true,
      opacity: 0.7,
      side: THREE.DoubleSide,
    });
    const progressRing = new THREE.Mesh(progressGeo, progressMat);
    progressRing.position.set(x, 0.03, z);
    this.scene.add(progressRing);

    // Floating indicator above desk
    const indicatorGeo = new THREE.BoxGeometry(0.3, 0.3, 0.3);
    const indicatorMat = new THREE.MeshBasicMaterial({
      color: 0x00cc66,
      transparent: true,
      opacity: 0.6,
    });
    const indicator = new THREE.Mesh(indicatorGeo, indicatorMat);
    indicator.position.set(x, 2.5, z);
    this.scene.add(indicator);

    this.stations.push({
      position,
      progress: 0,
      complete: false,
      ring,
      progressRing,
      progressGeo,
      indicator,
      indicatorMat,
    });
  }

  // Returns the nearest interactable station index, or -1
  getNearestStation(playerPos) {
    let bestDist = CONFIG.REFINE_INTERACT_RANGE;
    let bestIdx = -1;
    for (let i = 0; i < this.stations.length; i++) {
      if (this.stations[i].complete) continue;
      const d = playerPos.distanceTo(this.stations[i].position);
      if (d < bestDist) {
        bestDist = d;
        bestIdx = i;
      }
    }
    return bestIdx;
  }

  // Call each frame while player is interacting
  refine(stationIdx, dt) {
    const s = this.stations[stationIdx];
    if (s.complete) return;

    s.progress += dt / CONFIG.REFINE_TIME;
    if (s.progress >= 1) {
      s.progress = 1;
      s.complete = true;
      s.ring.material.opacity = 0.8;
      s.ring.material.color.setHex(0x00ff88);
      s.indicator.material.opacity = 0;
    }

    this._updateProgressRing(s);
  }

  // Decay progress slowly if player walks away mid-refine
  decayProgress(stationIdx, dt) {
    const s = this.stations[stationIdx];
    if (s.complete) return;
    if (s.progress > 0) {
      s.progress = Math.max(0, s.progress - dt * 0.1);
      this._updateProgressRing(s);
    }
  }

  _updateProgressRing(s) {
    // Rebuild ring geometry to show arc
    s.progressRing.geometry.dispose();
    const theta = s.progress * Math.PI * 2;
    const geo = new THREE.RingGeometry(2.0, 2.2, 32, 1, 0, theta);
    geo.rotateX(-Math.PI / 2);
    s.progressRing.geometry = geo;
  }

  update(dt, playerPos, isInteracting) {
    const nearIdx = this.getNearestStation(playerPos);

    for (let i = 0; i < this.stations.length; i++) {
      const s = this.stations[i];

      if (s.complete) continue;

      // Pulse the indicator
      s.indicator.position.y = 2.5 + Math.sin(Date.now() * 0.003) * 0.15;
      s.indicator.rotation.y += dt * 1.5;

      if (i === nearIdx && isInteracting) {
        this.refine(i, dt);
        s.ring.material.opacity = 0.6;
      } else {
        if (i !== nearIdx) {
          this.decayProgress(i, dt);
        }
        s.ring.material.opacity = 0.3;
      }
    }
  }

  get completedCount() {
    return this.stations.filter((s) => s.complete).length;
  }

  get allComplete() {
    return this.stations.every((s) => s.complete);
  }

  reset() {
    for (const s of this.stations) {
      s.progress = 0;
      s.complete = false;
      s.ring.material.opacity = 0.3;
      s.ring.material.color.setHex(0x00cc66);
      s.indicator.material.opacity = 0.6;
      this._updateProgressRing(s);
    }
  }
}
