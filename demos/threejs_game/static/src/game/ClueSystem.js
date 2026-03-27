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
  }

  get total() { return this._clues.size; }
  get solved() { return this._solvedCount; }

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
   * Speaks the clue prompt via TTS first, then opens a voice session
   * for the player's answer. Falls back to multiple-choice if unavailable.
   *
   * @param {string} id
   * @param {import('../network/VoiceClient.js').VoiceClient} voiceClient
   * @param {import('../network/TTSClient.js').TTSClient} [ttsClient]
   * @returns {Promise<boolean>}
   */
  async attemptSolveVoice(id, voiceClient, ttsClient) {
    const clue = this._clues.get(id);
    if (!clue || clue.solved) return false;

    // Fallback to multiple-choice if no voice client
    if (!voiceClient) return this.attemptSolve(id);

    // Show voice panel and speak the clue prompt via TTS (awaited)
    const panelPromise = this._ui.showVoicePanel(clue.question);

    if (ttsClient) {
      await ttsClient.speak(clue.question, 'Emma');
    }

    let solved = false;

    try {
      // Connect voice session for the player's answer
      await voiceClient.connect(id);

      // Wait for the panel to close (user cancel) or solve
      solved = await panelPromise;
    } catch (err) {
      console.error('Voice session error:', err);
      this._ui.hideVoicePanel();
      // Fallback to multiple-choice on error
      return this.attemptSolve(id);
    } finally {
      voiceClient.disconnect();
    }

    if (solved) {
      clue.solved = true;
      this._solvedCount++;
      this._updateCounter();
    }

    return solved;
  }

  _updateCounter() {
    this._ui.updateClueCounter(this._solvedCount, this._clues.size);
  }
}
