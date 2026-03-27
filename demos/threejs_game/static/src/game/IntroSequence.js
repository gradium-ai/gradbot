/**
 * Orchestrates the game start sequence:
 *  1. Fade in from black
 *  2. Show intro text card (+ optional TTS narration)
 *  3. Milchick greeting (talk animation + subtitle + optional TTS)
 *  4. Hand control back to the player
 *
 * Returns a promise that resolves when the intro is complete.
 */
// Back-left desk chair (actual chair mesh position in the desk island)
const CHAIR_POS = { x: -0.9, y: 0, z: 2.0 };
const CHAIR_FACING = Math.PI; // facing -Z toward the desk
// After standing, reposition outside the desk collision box (spans z ±2.5, player radius 0.35)
const STAND_POS = { x: -0.9, y: 0, z: 3.0 };

export class IntroSequence {
  /**
   * @param {import('../ui/GameUI.js').GameUI} ui
   * @param {import('./Milchick.js').Milchick} milchick
   * @param {import('../network/TTSClient.js').TTSClient} [ttsClient]
   * @param {import('./PlayerAnimator.js').PlayerAnimator} [playerAnimator]
   * @param {THREE.Object3D} [playerModel]
   */
  constructor(ui, milchick, ttsClient = null, playerAnimator = null, playerModel = null) {
    this._ui = ui;
    this._milchick = milchick;
    this._tts = ttsClient;
    this._animator = playerAnimator;
    this._model = playerModel;
  }

  /**
   * Run the full intro sequence.
   * @returns {Promise<void>}  Resolves when player should regain control
   */
  async play() {
    const INTRO_TEXT = 'You have suddenly woken up in an office called Gradium. Find the hidden clues without Neil noticing.';
    const MILCHICK_LINE = 'Welcome back, Laurent. Your outie has agreed to this arrangement. Please begin your work.';

    // Position Mark at the desk chair and start typing
    if (this._model) {
      this._model.position.set(CHAIR_POS.x, CHAIR_POS.y, CHAIR_POS.z);
      this._model.rotation.y = CHAIR_FACING;
    }
    if (this._animator) {
      this._animator.playSeated();
    }

    // 1. Fade in from black
    await this._ui.fadeIn(2000);

    // 2. Intro text card — show immediately, TTS enhances with audio
    this._ui.showIntroCard(INTRO_TEXT, 0);
    let introAudioPlayed = false;

    if (this._tts) {
      try {
        await this._tts.speak(INTRO_TEXT, 'Emma', {
          onFirstAudio: () => { introAudioPlayed = true; },
        });
      } catch (e) {
        console.error('[Intro] TTS error:', e);
      }
    }

    if (!introAudioPlayed) {
      await _wait(4500);
    }
    this._ui.hideIntroCard();

    // 3. Milchick greeting — show subtitle immediately, TTS enhances
    this._milchick.startTalking();
    this._ui.showSubtitle('Neil', `"${MILCHICK_LINE}"`, 0);
    let greetingAudioPlayed = false;

    if (this._tts) {
      try {
        await this._tts.speak(MILCHICK_LINE, 'Jack', {
          onFirstAudio: () => { greetingAudioPlayed = true; },
        });
      } catch (e) {
        console.error('[Intro] TTS error:', e);
      }
    }

    if (!greetingAudioPlayed) {
      await _wait(5000);
    }
    this._ui.hideSubtitle();

    this._milchick.stopTalking();

    // 4. Mark stands up from desk, then reposition outside collision box
    if (this._animator) {
      await this._animator.playSitToStand();
      if (this._model) {
        this._model.position.set(STAND_POS.x, STAND_POS.y, STAND_POS.z);
      }
      this._animator.transitionToGameplay();
    }

    // Small pause before handing off control
    await _wait(500);
  }
}

function _wait(ms) {
  return new Promise(r => setTimeout(r, ms));
}
