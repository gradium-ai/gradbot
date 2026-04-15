export class MenuScreen {
  constructor(overlay) {
    this.overlay = overlay;
    this._onStartCb = null;
    this._onResumeCb = null;
    this._onRestartCb = null;

    // Menu
    this._menu = this._createPanel('menu');
    this._menu.innerHTML = `
      <div style="font-size: 28px; letter-spacing: 6px; margin-bottom: 8px;">MACRODATA</div>
      <div style="font-size: 20px; letter-spacing: 4px; margin-bottom: 40px; color: #00cc66;">REFINEMENT</div>
      <div style="font-size: 11px; letter-spacing: 2px; margin-bottom: 30px; opacity: 0.6;">GRADIUM INDUSTRIES</div>
    `;
    const startBtn = this._createButton('BEGIN SHIFT');
    startBtn.addEventListener('click', () => this._onStartCb && this._onStartCb());
    this._menu.appendChild(startBtn);
    overlay.register('menu', this._menu);

    // Pause
    this._pause = this._createPanel('pause');
    this._pause.innerHTML = `
      <div style="font-size: 22px; letter-spacing: 4px; margin-bottom: 30px;">SHIFT PAUSED</div>
    `;
    const resumeBtn = this._createButton('RESUME');
    resumeBtn.addEventListener('click', () => this._onResumeCb && this._onResumeCb());
    this._pause.appendChild(resumeBtn);
    overlay.register('pause', this._pause);

    // Game Over
    this._gameOver = this._createPanel('gameOver');
    this._gameOverTitle = document.createElement('div');
    this._gameOverTitle.style.cssText = `
      font-size: 22px; letter-spacing: 4px; margin-bottom: 20px;
    `;
    this._gameOver.appendChild(this._gameOverTitle);
    this._gameOverReason = document.createElement('div');
    this._gameOverReason.style.cssText = `
      font-size: 13px; margin-bottom: 30px; max-width: 400px;
      line-height: 1.6; opacity: 0.8;
    `;
    this._gameOver.appendChild(this._gameOverReason);
    const restartBtn = this._createButton('RETURN TO LOBBY');
    restartBtn.addEventListener('click', () => this._onRestartCb && this._onRestartCb());
    this._gameOver.appendChild(restartBtn);
    overlay.register('gameOver', this._gameOver);

    this.hideAll();
  }

  _createPanel() {
    const el = document.createElement('div');
    el.style.cssText = `
      position: absolute;
      top: 0; left: 0; width: 100%; height: 100%;
      display: flex;
      flex-direction: column;
      align-items: center;
      justify-content: center;
      background: rgba(0, 0, 0, 0.85);
      font-family: 'Courier New', monospace;
      color: #00ff88;
    `;
    return el;
  }

  _createButton(text) {
    const btn = document.createElement('button');
    btn.textContent = text;
    btn.style.cssText = `
      font-family: 'Courier New', monospace;
      font-size: 14px;
      letter-spacing: 3px;
      color: #00ff88;
      background: transparent;
      border: 1px solid #00ff88;
      padding: 12px 32px;
      cursor: pointer;
      transition: background 0.2s, color 0.2s;
    `;
    btn.addEventListener('mouseenter', () => {
      btn.style.background = '#00ff88';
      btn.style.color = '#000';
    });
    btn.addEventListener('mouseleave', () => {
      btn.style.background = 'transparent';
      btn.style.color = '#00ff88';
    });
    return btn;
  }

  showMenu() { this.hideAll(); this._menu.style.display = 'flex'; }
  showPause() { this.hideAll(); this._pause.style.display = 'flex'; }
  showGameOver(reason, won = false) {
    this.hideAll();
    if (won) {
      this._gameOverTitle.textContent = 'SHIFT COMPLETE';
      this._gameOverTitle.style.color = '#00ff88';
    } else {
      this._gameOverTitle.textContent = 'SHIFT TERMINATED';
      this._gameOverTitle.style.color = '#ff3333';
    }
    this._gameOverReason.textContent = reason || '';
    this._gameOver.style.display = 'flex';
  }

  hideAll() {
    this._menu.style.display = 'none';
    this._pause.style.display = 'none';
    this._gameOver.style.display = 'none';
  }

  onStart(cb) { this._onStartCb = cb; }
  onResume(cb) { this._onResumeCb = cb; }
  onRestart(cb) { this._onRestartCb = cb; }
}
