import * as THREE from 'three';
import { CONFIG } from '../config.js';

export class GameLoop {
  constructor(updateFn) {
    this._clock = new THREE.Clock();
    this._updateFn = updateFn;
    this._running = false;
  }

  start() {
    this._running = true;
    this._clock.start();
    this._tick();
  }

  stop() {
    this._running = false;
  }

  _tick() {
    if (!this._running) return;
    requestAnimationFrame(() => this._tick());

    let dt = this._clock.getDelta();
    if (dt > CONFIG.MAX_DELTA) dt = CONFIG.MAX_DELTA;

    this._updateFn(dt);
  }
}
