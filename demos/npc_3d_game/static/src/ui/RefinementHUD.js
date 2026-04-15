import { CONFIG } from '../config.js';

export class RefinementHUD {
  constructor(overlay) {
    this.overlay = overlay;

    this.el = document.createElement('div');
    this.el.style.cssText = `
      position: absolute;
      bottom: 16px;
      left: 50%;
      transform: translateX(-50%);
      display: flex;
      flex-direction: column;
      align-items: center;
      gap: 8px;
      pointer-events: none;
    `;

    // Interaction prompt
    this._prompt = document.createElement('div');
    this._prompt.style.cssText = `
      font-family: 'Courier New', monospace;
      font-size: 14px;
      color: #00ff88;
      background: rgba(0, 0, 0, 0.7);
      padding: 6px 16px;
      border: 1px solid #00ff88;
      letter-spacing: 1px;
      display: none;
    `;
    this._prompt.textContent = 'PRESS E TO REFINE DATA';
    this.el.appendChild(this._prompt);

    // Progress boxes row
    this._boxRow = document.createElement('div');
    this._boxRow.style.cssText = `
      display: flex;
      gap: 8px;
      align-items: center;
    `;
    this.el.appendChild(this._boxRow);

    this._label = document.createElement('div');
    this._label.style.cssText = `
      font-family: 'Courier New', monospace;
      font-size: 11px;
      color: #00ff88;
      letter-spacing: 1px;
      margin-right: 8px;
    `;
    this._label.textContent = 'MDR:';
    this._boxRow.appendChild(this._label);

    this._boxes = [];
    for (let i = 0; i < CONFIG.DESK_COUNT; i++) {
      const box = document.createElement('div');
      box.style.cssText = `
        width: 24px;
        height: 24px;
        border: 2px solid #00cc66;
        background: transparent;
        transition: background 0.3s;
      `;
      this._boxRow.appendChild(box);
      this._boxes.push(box);
    }

    overlay.register('refinementHUD', this.el);
  }

  updateBoxes(stations) {
    for (let i = 0; i < stations.length && i < this._boxes.length; i++) {
      const s = stations[i];
      if (s.complete) {
        this._boxes[i].style.background = '#00ff88';
        this._boxes[i].style.borderColor = '#00ff88';
      } else if (s.progress > 0) {
        const pct = Math.floor(s.progress * 100);
        this._boxes[i].style.background = `linear-gradient(to top, #00cc66 ${pct}%, transparent ${pct}%)`;
        this._boxes[i].style.borderColor = '#00cc66';
      } else {
        this._boxes[i].style.background = 'transparent';
        this._boxes[i].style.borderColor = '#00cc66';
      }
    }
  }

  showPrompt(refining) {
    this._prompt.style.display = 'block';
    this._prompt.textContent = refining ? 'REFINING DATA...' : 'PRESS E TO REFINE DATA';
    if (refining) {
      this._prompt.style.borderColor = '#00ff88';
      this._prompt.style.color = '#00ff88';
    } else {
      this._prompt.style.borderColor = '#00cc66';
      this._prompt.style.color = '#00cc66';
    }
  }

  hidePrompt() {
    this._prompt.style.display = 'none';
  }

  show() { this.overlay.show('refinementHUD'); }
  hide() { this.overlay.hide('refinementHUD'); }

  reset() {
    for (const box of this._boxes) {
      box.style.background = 'transparent';
      box.style.borderColor = '#00cc66';
    }
    this.hidePrompt();
  }
}
