/**
 * Tracks clue discovery and puzzle solving.
 *
 * Each clue has:
 *  - id: unique key
 *  - name: display name for interaction tooltip
 *  - question, options, correctIndex: puzzle data
 *  - solved: boolean
 *
 * Usage:
 *   const clues = new ClueSystem(ui);
 *   clues.addClue({ id: 'note', name: 'Strange Note', ... });
 *   // when player interacts:
 *   await clues.attemptSolve('note');
 */
export class ClueSystem {
  /**
   * @param {import('../ui/GameUI.js').GameUI} ui
   */
  constructor(ui) {
    this._ui = ui;
    /** @type {Map<string, ClueData>} */
    this._clues = new Map();
    this._solvedCount = 0;
    this._inVoiceSession = false;
    this._activeVoiceClient = null;
  }

  get total() { return this._clues.size; }
  get solved() { return this._solvedCount; }
  get inVoiceSession() { return this._inVoiceSession; }

  /**
   * Register a clue.
   * @param {object} clue
   * @param {string} clue.id
   * @param {string} clue.name
   * @param {string} clue.question
   * @param {string[]} clue.options
   * @param {number} clue.correctIndex
   */
  addClue(clue) {
    this._clues.set(clue.id, { ...clue, solved: false });
    this._updateCounter();
  }

  /** Is a specific clue already solved? */
  isSolved(id) {
    return this._clues.get(id)?.solved ?? false;
  }

  /**
   * Show the puzzle prompt for a clue. Resolves when the player answers.
   * @param {string} id
   * @returns {Promise<boolean>}  true if solved correctly
   */
  async attemptSolve(id) {
    const clue = this._clues.get(id);
    if (!clue || clue.solved) return false;

    const correct = await this._ui.showPuzzle(
      clue.question,
      clue.options,
      clue.correctIndex
    );

    if (correct) {
      clue.solved = true;
      this._solvedCount++;
      this._updateCounter();
    }

    return correct;
  }

  /**
   * Voice-driven clue solving via the backend.
   * Opens a single voice session where the AI asks the question, listens
   * for the player's answer, and supports multi-turn conversation with hints.
   * Falls back to multiple-choice if voice is unavailable.
   *
   * @param {string} id
   * @param {import('../network/VoiceClient.js').VoiceClient} voiceClient
   * @returns {Promise<boolean>}
   */
  async attemptSolveVoice(id, voiceClient) {
    const clue = this._clues.get(id);
    if (!clue || clue.solved) return false;

    // Fallback to multiple-choice if no voice client
    if (!voiceClient) return this.attemptSolve(id);

    // Show voice panel — the AI will speak the question via the voice session
    const panelPromise = this._ui.showVoicePanel();

    let solved = false;
    this._inVoiceSession = true;
    this._activeVoiceClient = voiceClient;

    try {
      await voiceClient.connect(id);
      solved = await panelPromise;
    } catch (err) {
      console.error('Voice session error:', err);
      this._ui.hideVoicePanel();
      return this.attemptSolve(id);
    } finally {
      voiceClient.disconnect();
      this._inVoiceSession = false;
      this._activeVoiceClient = null;
    }

    if (solved) {
      clue.solved = true;
      this._solvedCount++;
      this._updateCounter();
    }

    return solved;
  }

  /**
   * Force-cancel an active voice session (e.g. when Neil arrives).
   * The panel closes and attemptSolveVoice resolves with false.
   */
  cancelVoiceSession() {
    if (!this._inVoiceSession) return;
    if (this._activeVoiceClient) {
      this._activeVoiceClient.disconnect();
      this._activeVoiceClient = null;
    }
    this._ui.hideVoicePanel();
    this._inVoiceSession = false;
  }

  _updateCounter() {
    this._ui.updateClueCounter(this._solvedCount, this._clues.size);
  }
}
