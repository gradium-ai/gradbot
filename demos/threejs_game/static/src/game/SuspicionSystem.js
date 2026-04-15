import { CONFIG } from '../config.js';

/**
 * Tracks Milchick's suspicion of Mark on a 0–3 scale.
 *
 *   0 = calm
 *   1 = mildly suspicious
 *   2 = highly suspicious
 *   3 = caught / fail state
 *
 * Also tracks contextual risk signals (is the player currently
 * interacting with a clue? lingering near a suspicious location?).
 */
export class SuspicionSystem {
  constructor() {
    this._level = 0;

    /** True while the player is in a clue voice/puzzle session */
    this.playerInClueInteraction = false;

    /** Set to a clue trigger position when the player is near one */
    this.playerNearClue = false;

    /** True when the player is sneaking (reduces suspicion gain) */
    this.playerSneaking = false;

    /** @type {((level: number) => void)|null} */
    this.onLevelChange = null;

    /** @type {(() => void)|null} */
    this.onCaught = null;
  }

  get level() { return this._level; }

  /**
   * Raise suspicion by an integer amount. Clamps to SUSPICION_MAX.
   * @param {number} amount
   */
  raise(amount = 1) {
    if (this.playerSneaking) {
      amount = Math.max(0, Math.round(amount * CONFIG.SNEAK_SUSPICION_MULT));
      if (amount <= 0) return;
    }
    const prev = this._level;
    this._level = Math.min(CONFIG.SUSPICION_MAX, this._level + amount);
    if (this._level !== prev) {
      this.onLevelChange?.(this._level);
      if (this._level >= CONFIG.SUSPICION_MAX) {
        this.onCaught?.();
      }
    }
  }

  /**
   * Lower suspicion by an integer amount. Clamps to 0.
   * @param {number} amount
   */
  lower(amount = 1) {
    const prev = this._level;
    this._level = Math.max(0, this._level - amount);
    if (this._level !== prev) {
      this.onLevelChange?.(this._level);
    }
  }

  /**
   * Get the current risk modifier for a check-in.
   * Returns 0 (safe), 1 (moderate), or 2 (high risk).
   */
  getRiskModifier() {
    if (this.playerInClueInteraction) return 2;
    if (this.playerNearClue && !this.playerSneaking) return 1;
    if (this.playerNearClue && this.playerSneaking) return 0;
    return 0;
  }

  reset() {
    this._level = 0;
    this.playerInClueInteraction = false;
    this.playerNearClue = false;
    this.playerSneaking = false;
    this.onLevelChange?.(0);
  }
}
