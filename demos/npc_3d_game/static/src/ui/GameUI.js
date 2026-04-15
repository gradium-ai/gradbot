/**
 * In-game HUD: subtitle box, objective text, clue counter, fade overlay.
 * All DOM-based, layered over the canvas.
 */
export class GameUI {
  constructor() {
    this._container = document.createElement('div');
    this._container.id = 'game-ui';
    this._container.style.cssText = `
      position: fixed; inset: 0;
      pointer-events: none;
      z-index: 60;
      font-family: 'Courier New', monospace;
    `;
    document.body.appendChild(this._container);

    this._buildFadeOverlay();
    this._buildIntroCard();
    this._buildSubtitleBox();
    this._buildObjective();
    this._buildClueCounter();
    this._buildTimer();
    this._buildPuzzlePrompt();
    this._buildVoicePanel();
    this._buildSuspicionIndicator();
    this._buildControlsHint();
    this._buildGameOver();
    this._buildVictory();
  }

  // ── Fade overlay ────────────────────────────────────────────

  _buildFadeOverlay() {
    this._fade = document.createElement('div');
    this._fade.style.cssText = `
      position: fixed; inset: 0;
      background: #000;
      opacity: 1;
      transition: opacity 1.5s ease;
      z-index: 200;
      pointer-events: none;
    `;
    this._container.appendChild(this._fade);
  }

  fadeIn(durationMs = 1500) {
    this._fade.style.transition = `opacity ${durationMs}ms ease`;
    this._fade.style.opacity = '0';
    return new Promise(r => setTimeout(r, durationMs));
  }

  fadeOut(durationMs = 1000) {
    this._fade.style.transition = `opacity ${durationMs}ms ease`;
    this._fade.style.opacity = '1';
    return new Promise(r => setTimeout(r, durationMs));
  }

  // ── Intro text card ─────────────────────────────────────────

  _buildIntroCard() {
    this._introCard = document.createElement('div');
    this._introCard.style.cssText = `
      position: fixed; inset: 0;
      display: flex; align-items: center; justify-content: center;
      z-index: 190;
      pointer-events: none;
      opacity: 0;
      transition: opacity 1s ease;
    `;
    this._introCard.innerHTML = `
      <div style="
        max-width: 520px;
        text-align: center;
        color: #00cc66;
        font-size: 16px;
        line-height: 1.8;
        letter-spacing: 1px;
        padding: 32px;
        border: 1px solid rgba(0,204,102,0.3);
        background: rgba(0,0,0,0.85);
      "></div>
    `;
    this._container.appendChild(this._introCard);
  }

  showIntroCard(text, durationMs = 4000) {
    this._introCard.querySelector('div').textContent = text;
    this._introCard.style.opacity = '1';
    if (durationMs > 0) {
      return new Promise(r => {
        setTimeout(() => {
          this._introCard.style.opacity = '0';
          setTimeout(r, 1000);
        }, durationMs);
      });
    }
  }

  hideIntroCard() {
    this._introCard.style.opacity = '0';
  }

  // ── Subtitle box ────────────────────────────────────────────

  _buildSubtitleBox() {
    this._subtitle = document.createElement('div');
    this._subtitle.style.cssText = `
      position: fixed; bottom: 80px; left: 50%;
      transform: translateX(-50%);
      max-width: 600px;
      padding: 10px 24px;
      background: rgba(0, 0, 0, 0.75);
      color: #e0e0e0;
      font-size: 15px;
      line-height: 1.6;
      letter-spacing: 0.5px;
      border-left: 3px solid #00cc66;
      display: none;
      z-index: 70;
    `;
    this._container.appendChild(this._subtitle);
  }

  showSubtitle(speaker, text, durationMs = 4000) {
    this._subtitle.textContent = '';
    const span = document.createElement('span');
    span.style.cssText = 'color:#00cc66;font-weight:bold;';
    span.textContent = `${speaker}: `;
    this._subtitle.appendChild(span);
    this._subtitle.appendChild(document.createTextNode(text));
    this._subtitle.style.display = 'block';
    if (durationMs > 0) {
      clearTimeout(this._subTimer);
      this._subTimer = setTimeout(() => this.hideSubtitle(), durationMs);
    }
  }

  hideSubtitle() {
    this._subtitle.style.display = 'none';
  }

  // ── Objective text ──────────────────────────────────────────

  _buildObjective() {
    this._objective = document.createElement('div');
    this._objective.style.cssText = `
      position: fixed; top: 24px; left: 24px;
      color: #000;
      font-size: 13px;
      letter-spacing: 1px;
      display: none;
      z-index: 70;
    `;
    this._container.appendChild(this._objective);
  }

  setObjective(text) {
    this._objective.textContent = text;
    this._objective.style.display = 'block';
  }

  hideObjective() {
    this._objective.style.display = 'none';
  }

  // ── Clue counter ────────────────────────────────────────────

  _buildClueCounter() {
    this._clueCounter = document.createElement('div');
    this._clueCounter.style.cssText = `
      position: fixed; top: 24px; right: 24px;
      color: #00cc66;
      font-size: 14px;
      letter-spacing: 1px;
      display: none;
      z-index: 70;
    `;
    this._container.appendChild(this._clueCounter);
  }

  updateClueCounter(found, total) {
    this._clueCounter.textContent = `CLUES: ${found} / ${total}`;
    this._clueCounter.style.display = 'block';
  }

  // ── Timer ──────────────────────────────────────────────────

  _buildTimer() {
    this._timer = document.createElement('div');
    this._timer.style.cssText = `
      position: fixed; top: 24px; left: 50%;
      transform: translateX(-50%);
      color: #ff3333;
      font-size: 22px;
      font-weight: bold;
      letter-spacing: 3px;
      display: none;
      z-index: 70;
      text-shadow: 0 0 8px rgba(255, 51, 51, 0.5);
    `;
    this._container.appendChild(this._timer);
  }

  updateTimer(secondsLeft) {
    const min = Math.floor(secondsLeft / 60);
    const sec = secondsLeft % 60;
    this._timer.textContent = `${min}:${sec.toString().padStart(2, '0')}`;
    this._timer.style.display = 'block';

    // Pulse when under 30 seconds
    if (secondsLeft <= 30) {
      this._timer.style.animation = 'none';
      this._timer.offsetHeight; // reflow
      this._timer.animate([
        { opacity: 1 }, { opacity: 0.4 }, { opacity: 1 },
      ], { duration: 600 });
    }
  }

  hideTimer() {
    this._timer.style.display = 'none';
  }

  // ── Puzzle prompt ───────────────────────────────────────────

  _buildPuzzlePrompt() {
    this._puzzle = document.createElement('div');
    this._puzzle.style.cssText = `
      position: fixed; inset: 0;
      display: none;
      align-items: center; justify-content: center;
      z-index: 150;
      pointer-events: auto;
    `;
    this._puzzle.innerHTML = `
      <div style="
        background: rgba(0,0,0,0.9);
        border: 1px solid #00cc66;
        padding: 32px;
        max-width: 440px;
        text-align: center;
        color: #e0e0e0;
        font-family: 'Courier New', monospace;
      ">
        <div id="puzzle-question" style="font-size:15px;line-height:1.6;margin-bottom:20px;"></div>
        <div id="puzzle-options" style="display:flex;flex-direction:column;gap:8px;"></div>
        <div id="puzzle-result" style="margin-top:16px;font-size:14px;display:none;"></div>
      </div>
    `;
    this._container.appendChild(this._puzzle);
  }

  /**
   * Show a multiple-choice puzzle.
   * @param {string} question
   * @param {string[]} options
   * @param {number} correctIndex  0-based index of the correct answer
   * @returns {Promise<boolean>}  true if answered correctly
   */
  // ── Voice clue panel ────────────────────────────────────────

  _buildVoicePanel() {
    this._voicePanel = document.createElement('div');
    this._voicePanel.style.cssText = `
      position: fixed; inset: 0;
      display: none;
      align-items: center; justify-content: center;
      z-index: 150;
      pointer-events: auto;
    `;
    this._voicePanel.innerHTML = `
      <div style="
        background: rgba(0,0,0,0.92);
        border: 1px solid #00cc66;
        padding: 28px 32px;
        max-width: 480px;
        width: 90%;
        font-family: 'Courier New', monospace;
        color: #e0e0e0;
      ">
        <div id="voice-transcript" style="
          min-height: 100px; max-height: 200px;
          overflow-y: auto;
          border: 1px solid rgba(0,204,102,0.2);
          padding: 10px;
          margin-bottom: 12px;
          font-size: 13px;
          line-height: 1.5;
        "></div>
        <div style="display: flex; align-items: center; justify-content: space-between;">
          <div id="voice-mic-indicator" style="
            color: #00cc66; font-size: 13px;
          ">MIC ACTIVE</div>
          <div id="voice-result" style="
            font-size: 14px; display: none;
          "></div>
          <button id="voice-close-btn" style="
            background: transparent;
            border: 1px solid rgba(0,204,102,0.4);
            color: #00cc66;
            padding: 6px 16px;
            font-family: 'Courier New', monospace;
            font-size: 13px;
            cursor: pointer;
          ">CLOSE</button>
        </div>
      </div>
    `;
    this._container.appendChild(this._voicePanel);
  }

  /**
   * Show the voice panel (chat-only, no title).
   * Returns a Promise that resolves with true (solved) or false (cancelled).
   * Call addVoiceTranscript / showVoiceResult while the panel is open.
   * @returns {Promise<boolean>}
   */
  showVoicePanel() {
    return new Promise((resolve) => {
      document.exitPointerLock();

      this._voicePanel.style.display = 'flex';
      this._voicePanel.querySelector('#voice-transcript').innerHTML = '';
      this._lastTranscriptLine = null;
      this._lastTranscriptIsUser = null;
      const resultEl = this._voicePanel.querySelector('#voice-result');
      resultEl.style.display = 'none';

      this._voiceResolve = resolve;

      const closeBtn = this._voicePanel.querySelector('#voice-close-btn');
      closeBtn.onclick = () => {
        this._voicePanel.style.display = 'none';
        this._voiceResolve = null;
        resolve(false);
      };
    });
  }

  /**
   * Append a line to the voice transcript area.
   * @param {string} text
   * @param {boolean} isUser
   */
  addVoiceTranscript(text, isUser) {
    const el = this._voicePanel.querySelector('#voice-transcript');

    // Accumulate into the current line if same speaker, otherwise start a new line
    if (this._lastTranscriptIsUser === isUser && this._lastTranscriptLine) {
      // Append to existing speaker line
      this._lastTranscriptLine.dataset.text += ' ' + text;
      // Re-render safely: clear and rebuild with DOM nodes
      this._lastTranscriptLine.textContent = '';
      const labelSpan = document.createElement('span');
      labelSpan.style.color = isUser ? '#aaa' : '#00cc66';
      labelSpan.textContent = isUser ? 'You: ' : 'Voice: ';
      this._lastTranscriptLine.appendChild(labelSpan);
      this._lastTranscriptLine.appendChild(document.createTextNode(this._lastTranscriptLine.dataset.text));
    } else {
      // New speaker — create a new line
      const line = document.createElement('div');
      line.style.marginBottom = '4px';
      line.dataset.text = text;
      const labelSpan = document.createElement('span');
      labelSpan.style.color = isUser ? '#aaa' : '#00cc66';
      labelSpan.textContent = isUser ? 'You: ' : 'Voice: ';
      line.appendChild(labelSpan);
      line.appendChild(document.createTextNode(text));
      el.appendChild(line);
      this._lastTranscriptLine = line;
      this._lastTranscriptIsUser = isUser;
    }

    el.scrollTop = el.scrollHeight;
  }

  /**
   * Show a result in the voice panel.
   * Handles 3 states: correct, wrong with retries, attempts exhausted.
   * @param {boolean} correct
   * @param {string} [fragment]
   * @param {boolean} [attemptsExhausted]
   */
  showVoiceResult(correct, fragment, attemptsExhausted = false) {
    console.log('[GameUI] showVoiceResult:', correct, 'exhausted:', attemptsExhausted);
    const resultEl = this._voicePanel.querySelector('#voice-result');

    if (correct) {
      resultEl.textContent = 'CORRECT. Clue recorded.';
      resultEl.style.color = '#00cc66';
      resultEl.style.display = 'block';
      if (this._voiceResolve) {
        const resolve = this._voiceResolve;
        this._voiceResolve = null;
        resolve(true);
        setTimeout(() => { this._voicePanel.style.display = 'none'; }, 3000);
      }
    } else if (attemptsExhausted) {
      resultEl.textContent = 'Maximum attempts reached.';
      resultEl.style.color = '#cc4444';
      resultEl.style.display = 'block';
      if (this._voiceResolve) {
        const resolve = this._voiceResolve;
        this._voiceResolve = null;
        resolve(false);
        setTimeout(() => { this._voicePanel.style.display = 'none'; }, 3000);
      }
    } else {
      // Wrong but retries remain — flash briefly, panel stays open
      resultEl.textContent = 'Incorrect. Try again.';
      resultEl.style.color = '#cc4444';
      resultEl.style.display = 'block';
      setTimeout(() => { resultEl.style.display = 'none'; }, 2000);
    }
  }

  hideVoicePanel() {
    this._voicePanel.style.display = 'none';
    if (this._voiceResolve) {
      this._voiceResolve(false);
      this._voiceResolve = null;
    }
  }

  // ── Controls hint ─────────────────────────────────────────

  _buildControlsHint() {
    this._controlsHint = document.createElement('div');
    this._controlsHint.style.cssText = `
      position: fixed; bottom: 24px; left: 24px;
      background: rgba(0, 0, 0, 0.7);
      border: 1px solid rgba(0,204,102,0.3);
      padding: 12px 18px;
      color: rgba(255,255,255,0.7);
      font-size: 12px;
      line-height: 1.8;
      letter-spacing: 0.5px;
      display: none;
      z-index: 70;
      opacity: 1;
      transition: opacity 1.5s ease;
    `;
    this._controlsHint.innerHTML = `
      <div style="color:#00cc66;font-size:13px;margin-bottom:6px;letter-spacing:1px;">CONTROLS</div>
      <div><span style="color:#00cc66;">WASD</span> — Walk</div>
      <div><span style="color:#00cc66;">Shift + WASD</span> — Sprint</div>
      <div><span style="color:#00cc66;">C / Ctrl + WASD</span> — Sneak (reduces suspicion)</div>
      <div><span style="color:#00cc66;">Mouse</span> — Look around</div>
      <div><span style="color:#00cc66;">E</span> — Interact with objects</div>
    `;
    this._container.appendChild(this._controlsHint);
  }

  showControlsHint() {
    this._controlsHint.style.display = 'block';
    this._controlsHint.style.opacity = '1';

    const dismiss = () => {
      this._controlsHint.style.opacity = '0';
      setTimeout(() => { this._controlsHint.style.display = 'none'; }, 1500);
      window.removeEventListener('keydown', dismiss);
    };
    // Dismiss on first keypress
    window.addEventListener('keydown', dismiss);
  }

  // ── Suspicion indicator ────────────────────────────────────

  _buildSuspicionIndicator() {
    this._suspicionEl = document.createElement('div');
    this._suspicionEl.style.cssText = `
      position: fixed; top: 50px; right: 24px;
      color: #00cc66;
      font-size: 12px;
      letter-spacing: 1px;
      display: none;
      z-index: 70;
      transition: color 0.5s ease;
    `;
    this._container.appendChild(this._suspicionEl);
  }

  /**
   * Update the suspicion level display.
   * @param {number} level  0–3
   */
  updateSuspicion(level) {
    if (level <= 0) {
      this._suspicionEl.style.display = 'none';
      return;
    }

    this._suspicionEl.style.display = 'block';

    const labels = ['', 'MILD SUSPICION', 'HIGH SUSPICION', 'CAUGHT'];
    const colors = ['', '#ffcc00', '#ff6633', '#ff0000'];

    this._suspicionEl.textContent = labels[level] || '';
    this._suspicionEl.style.color = colors[level] || '#ffcc00';

    // Flash effect
    this._suspicionEl.style.opacity = '1';
    this._suspicionEl.animate([
      { opacity: 1 }, { opacity: 0.4 }, { opacity: 1 },
    ], { duration: 600 });
  }

  // ── Game over ─────────────────────────────────────────────

  _buildGameOver() {
    this._gameOver = document.createElement('div');
    this._gameOver.style.cssText = `
      position: fixed; inset: 0;
      display: none;
      align-items: center; justify-content: center;
      z-index: 250;
      pointer-events: auto;
      background: rgba(0, 0, 0, 0);
      transition: background 2s ease;
      flex-direction: column;
      gap: 16px;
      font-family: 'Courier New', monospace;
    `;
    this._gameOver.innerHTML = `
      <div style="
        color: #ff3333;
        font-size: 32px;
        font-weight: bold;
        letter-spacing: 6px;
        opacity: 0;
        transition: opacity 1.5s ease 0.5s;
      " id="gameover-title">COMPLIANCE BREACH</div>
      <div style="
        color: #cc4444;
        font-size: 14px;
        letter-spacing: 2px;
        opacity: 0;
        transition: opacity 1.5s ease 1s;
      " id="gameover-sub">Neil has reported your behavior to the Board.</div>
      <div style="
        color: #666;
        font-size: 13px;
        margin-top: 24px;
        opacity: 0;
        transition: opacity 1.5s ease 2s;
      " id="gameover-hint">Refresh to try again</div>
    `;
    this._container.appendChild(this._gameOver);
  }

  showGameOver(title, subtitle) {
    document.exitPointerLock();
    if (title) this._gameOver.querySelector('#gameover-title').textContent = title;
    if (subtitle) this._gameOver.querySelector('#gameover-sub').textContent = subtitle;
    this._gameOver.style.display = 'flex';

    // Trigger transitions
    requestAnimationFrame(() => {
      this._gameOver.style.background = 'rgba(0, 0, 0, 0.92)';
      this._gameOver.querySelector('#gameover-title').style.opacity = '1';
      this._gameOver.querySelector('#gameover-sub').style.opacity = '1';
      this._gameOver.querySelector('#gameover-hint').style.opacity = '1';
    });
  }

  // ── Victory screen (Music Dance Experience) ───────────────

  _buildVictory() {
    this._victory = document.createElement('div');
    this._victory.style.cssText = `
      position: fixed; inset: 0;
      display: none;
      align-items: center; justify-content: center;
      z-index: 250;
      pointer-events: auto;
      background: rgba(0, 0, 0, 0);
      transition: background 2s ease;
      flex-direction: column;
      gap: 16px;
      font-family: 'Courier New', monospace;
    `;
    this._victory.innerHTML = `
      <div style="
        color: #00cc66;
        font-size: 14px;
        letter-spacing: 4px;
        opacity: 0;
        transition: opacity 1.5s ease 0.3s;
      " id="victory-pre">GRADIUM INDUSTRIES PRESENTS</div>
      <div style="
        color: #00cc66;
        font-size: 36px;
        font-weight: bold;
        letter-spacing: 8px;
        opacity: 0;
        transition: opacity 1.5s ease 1s;
      " id="victory-title">MUSIC DANCE EXPERIENCE</div>
      <div style="
        color: rgba(0,204,102,0.6);
        font-size: 14px;
        letter-spacing: 2px;
        opacity: 0;
        transition: opacity 1.5s ease 1.8s;
      " id="victory-sub">All clues recovered. Your outie would be proud.</div>
      <div style="
        color: #444;
        font-size: 13px;
        margin-top: 32px;
        opacity: 0;
        transition: opacity 1.5s ease 3s;
      " id="victory-hint">Refresh to play again</div>
    `;
    this._container.appendChild(this._victory);
  }

  showVictory() {
    document.exitPointerLock();
    this._victory.style.display = 'flex';

    requestAnimationFrame(() => {
      this._victory.style.background = 'rgba(0, 0, 0, 0.85)';
      this._victory.querySelector('#victory-pre').style.opacity = '1';
      this._victory.querySelector('#victory-title').style.opacity = '1';
      this._victory.querySelector('#victory-sub').style.opacity = '1';
      this._victory.querySelector('#victory-hint').style.opacity = '1';
    });
  }

  // ── Puzzle prompt (debug fallback) ────────────────────────

  showPuzzle(question, options, correctIndex) {
    return new Promise((resolve) => {
      // Exit pointer lock so mouse can click options
      document.exitPointerLock();

      this._puzzle.style.display = 'flex';
      this._puzzle.querySelector('#puzzle-question').textContent = question;
      const resultEl = this._puzzle.querySelector('#puzzle-result');
      resultEl.style.display = 'none';

      const optionsEl = this._puzzle.querySelector('#puzzle-options');
      optionsEl.innerHTML = '';

      options.forEach((opt, i) => {
        const btn = document.createElement('button');
        btn.textContent = opt;
        btn.style.cssText = `
          background: transparent;
          border: 1px solid rgba(0,204,102,0.4);
          color: #00cc66;
          padding: 8px 16px;
          font-family: 'Courier New', monospace;
          font-size: 14px;
          cursor: pointer;
          transition: background 0.2s;
        `;
        btn.addEventListener('mouseenter', () => {
          btn.style.background = 'rgba(0,204,102,0.15)';
        });
        btn.addEventListener('mouseleave', () => {
          btn.style.background = 'transparent';
        });
        btn.addEventListener('click', () => {
          const correct = i === correctIndex;
          resultEl.textContent = correct ? 'CORRECT. Clue recorded.' : 'Incorrect. Try again later.';
          resultEl.style.color = correct ? '#00cc66' : '#cc4444';
          resultEl.style.display = 'block';

          setTimeout(() => {
            this._puzzle.style.display = 'none';
            resolve(correct);
          }, 1500);
        });
        optionsEl.appendChild(btn);
      });
    });
  }
}
