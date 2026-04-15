import { CONFIG } from '../config.js';

export class SuspicionMeter {
  constructor(overlay) {
    this.overlay = overlay;
    this.value = 0;

    this.el = document.createElement('div');
    this.el.style.cssText = `
      position: absolute;
      top: 16px;
      right: 16px;
      width: 160px;
      pointer-events: none;
    `;

    this._label = document.createElement('div');
    this._label.style.cssText = `
      font-family: 'Courier New', monospace;
      font-size: 11px;
      color: #00ff88;
      margin-bottom: 4px;
      letter-spacing: 1px;
    `;
    this._label.textContent = 'SUSPICION';
    this.el.appendChild(this._label);

    this._barBg = document.createElement('div');
    this._barBg.style.cssText = `
      width: 100%;
      height: 12px;
      background: rgba(0, 0, 0, 0.7);
      border: 1px solid #00ff88;
    `;
    this.el.appendChild(this._barBg);

    this._barFill = document.createElement('div');
    this._barFill.style.cssText = `
      width: 0%;
      height: 100%;
      background: #00ff88;
      transition: width 0.1s, background-color 0.3s;
    `;
    this._barBg.appendChild(this._barFill);

    overlay.register('suspicionMeter', this.el);
  }

  reset() {
    this.value = 0;
    this._render();
  }

  update(dt, detected) {
    if (detected) {
      this.value += CONFIG.SUSPICION_RISE_RATE * dt;
    } else {
      this.value -= CONFIG.SUSPICION_DECAY_RATE * dt;
    }
    this.value = Math.max(0, Math.min(CONFIG.SUSPICION_MAX, this.value));
    this._render();
  }

  show() { this.overlay.show('suspicionMeter'); }
  hide() { this.overlay.hide('suspicionMeter'); }

  _render() {
    const pct = (this.value / CONFIG.SUSPICION_MAX) * 100;
    this._barFill.style.width = `${pct}%`;

    if (pct < 40) {
      this._barFill.style.backgroundColor = '#00ff88';
    } else if (pct < 70) {
      this._barFill.style.backgroundColor = '#ffcc00';
    } else {
      this._barFill.style.backgroundColor = '#ff3333';
    }
  }
}
