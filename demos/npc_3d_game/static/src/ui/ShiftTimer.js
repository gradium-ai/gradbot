import { CONFIG } from '../config.js';

export class ShiftTimer {
  constructor(overlay) {
    this.overlay = overlay;
    this.timeLeft = CONFIG.SHIFT_DURATION;

    this.el = document.createElement('div');
    this.el.style.cssText = `
      position: absolute;
      top: 16px;
      left: 50%;
      transform: translateX(-50%);
      font-family: 'Courier New', monospace;
      font-size: 18px;
      color: #00ff88;
      background: rgba(0, 0, 0, 0.7);
      padding: 8px 20px;
      border: 1px solid #00ff88;
      letter-spacing: 2px;
      pointer-events: none;
    `;
    overlay.register('shiftTimer', this.el);
  }

  reset() {
    this.timeLeft = CONFIG.SHIFT_DURATION;
    this._render();
  }

  update(dt) {
    this.timeLeft -= dt;
    if (this.timeLeft < 0) this.timeLeft = 0;
    this._render();
  }

  show() { this.overlay.show('shiftTimer'); }
  hide() { this.overlay.hide('shiftTimer'); }

  _render() {
    const m = Math.floor(this.timeLeft / 60);
    const s = Math.floor(this.timeLeft % 60);
    const mm = String(m).padStart(2, '0');
    const ss = String(s).padStart(2, '0');
    this.el.textContent = `SHIFT REMAINING: ${mm}:${ss}`;
  }
}
