export class InputManager {
  constructor() {
    this._keys = {};
    this._touchDir = { x: 0, z: 0 };

    window.addEventListener('keydown', (e) => { this._keys[e.code] = true; });
    window.addEventListener('keyup', (e) => { this._keys[e.code] = false; });
  }

  setTouchDirection(x, z) {
    this._touchDir.x = x;
    this._touchDir.z = z;
  }

  getDirection() {
    let x = 0;
    let z = 0;

    if (this._keys['KeyW'] || this._keys['ArrowUp']) z -= 1;
    if (this._keys['KeyS'] || this._keys['ArrowDown']) z += 1;
    if (this._keys['KeyA'] || this._keys['ArrowLeft']) x -= 1;
    if (this._keys['KeyD'] || this._keys['ArrowRight']) x += 1;

    // Combine keyboard + touch
    x += this._touchDir.x;
    z += this._touchDir.z;

    // Normalize
    const len = Math.sqrt(x * x + z * z);
    if (len > 1) {
      x /= len;
      z /= len;
    }

    return { x, z };
  }

  setVirtualKey(code, pressed) {
    this._keys[code] = pressed;
  }

  isKeyDown(code) {
    return !!this._keys[code];
  }
}
