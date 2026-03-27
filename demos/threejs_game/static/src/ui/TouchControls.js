export class TouchControls {
  constructor(overlay, inputManager) {
    this._input = inputManager;
    this._active = false;
    this._touchId = null;
    this._originX = 0;
    this._originY = 0;

    // Only show on touch devices
    this._container = document.createElement('div');
    this._container.style.cssText = `
      position: absolute;
      bottom: 40px;
      left: 40px;
      width: 120px;
      height: 120px;
      border-radius: 50%;
      background: rgba(0, 255, 136, 0.15);
      border: 2px solid rgba(0, 255, 136, 0.3);
      display: none;
      touch-action: none;
    `;

    this._thumb = document.createElement('div');
    this._thumb.style.cssText = `
      position: absolute;
      width: 40px;
      height: 40px;
      border-radius: 50%;
      background: rgba(0, 255, 136, 0.5);
      top: 50%;
      left: 50%;
      transform: translate(-50%, -50%);
      pointer-events: none;
    `;
    this._container.appendChild(this._thumb);
    overlay.register('touchControls', this._container);

    // Action button (bottom-right) for refining data
    this._actionBtn = document.createElement('div');
    this._actionBtn.textContent = 'E';
    this._actionBtn.style.cssText = `
      position: absolute;
      bottom: 60px;
      right: 40px;
      width: 70px;
      height: 70px;
      border-radius: 50%;
      background: rgba(0, 255, 136, 0.15);
      border: 2px solid rgba(0, 255, 136, 0.3);
      display: none;
      touch-action: none;
      font-family: 'Courier New', monospace;
      font-size: 20px;
      color: #00ff88;
      line-height: 70px;
      text-align: center;
      user-select: none;
    `;
    this._actionBtn.addEventListener('touchstart', (e) => {
      e.preventDefault();
      this._input.setVirtualKey('KeyE', true);
      this._actionBtn.style.background = 'rgba(0, 255, 136, 0.4)';
    });
    this._actionBtn.addEventListener('touchend', (e) => {
      e.preventDefault();
      this._input.setVirtualKey('KeyE', false);
      this._actionBtn.style.background = 'rgba(0, 255, 136, 0.15)';
    });
    overlay.register('touchAction', this._actionBtn);

    // Show touch controls on first touch
    window.addEventListener('touchstart', () => {
      this._container.style.display = 'block';
      this._actionBtn.style.display = 'block';
    }, { once: true });

    this._container.addEventListener('touchstart', (e) => this._onTouchStart(e));
    this._container.addEventListener('touchmove', (e) => this._onTouchMove(e));
    this._container.addEventListener('touchend', (e) => this._onTouchEnd(e));
    this._container.addEventListener('touchcancel', (e) => this._onTouchEnd(e));
  }

  _onTouchStart(e) {
    e.preventDefault();
    const touch = e.changedTouches[0];
    this._touchId = touch.identifier;
    const rect = this._container.getBoundingClientRect();
    this._originX = rect.left + rect.width / 2;
    this._originY = rect.top + rect.height / 2;
    this._active = true;
  }

  _onTouchMove(e) {
    e.preventDefault();
    if (!this._active) return;
    for (const touch of e.changedTouches) {
      if (touch.identifier !== this._touchId) continue;

      const dx = touch.clientX - this._originX;
      const dy = touch.clientY - this._originY;
      const maxR = 50;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const clampedDist = Math.min(dist, maxR);
      const angle = Math.atan2(dy, dx);

      const nx = (clampedDist / maxR) * Math.cos(angle);
      const ny = (clampedDist / maxR) * Math.sin(angle);

      // Move thumb
      this._thumb.style.left = `${50 + (nx * 40)}%`;
      this._thumb.style.top = `${50 + (ny * 40)}%`;

      // Map to game direction (dy → z, dx → x)
      this._input.setTouchDirection(nx, ny);
    }
  }

  _onTouchEnd(e) {
    for (const touch of e.changedTouches) {
      if (touch.identifier !== this._touchId) continue;
      this._active = false;
      this._touchId = null;
      this._thumb.style.left = '50%';
      this._thumb.style.top = '50%';
      this._input.setTouchDirection(0, 0);
    }
  }
}
