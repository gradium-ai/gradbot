const VALID_TRANSITIONS = {
  MENU: ['PLAYING'],
  PLAYING: ['PAUSED', 'GAME_OVER'],
  PAUSED: ['PLAYING', 'MENU'],
  GAME_OVER: ['MENU'],
};

export class GameStateMachine {
  constructor() {
    this.state = 'MENU';
    this._listeners = {};
  }

  on(event, fn) {
    if (!this._listeners[event]) this._listeners[event] = [];
    this._listeners[event].push(fn);
  }

  _emit(event, ...args) {
    const fns = this._listeners[event];
    if (fns) fns.forEach((fn) => fn(...args));
  }

  transition(newState, ...args) {
    const allowed = VALID_TRANSITIONS[this.state];
    if (!allowed || !allowed.includes(newState)) return;

    const old = this.state;
    this._emit(`leave_${old}`, ...args);
    this.state = newState;
    this._emit(`enter_${newState}`, ...args);
  }
}
