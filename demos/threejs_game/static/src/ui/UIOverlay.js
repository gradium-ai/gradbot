export class UIOverlay {
  constructor(rootEl) {
    this.root = rootEl;
    this._elements = {};
  }

  register(name, el) {
    this._elements[name] = el;
    this.root.appendChild(el);
  }

  show(name) {
    const el = this._elements[name];
    if (el) el.style.display = '';
  }

  hide(name) {
    const el = this._elements[name];
    if (el) el.style.display = 'none';
  }

  get(name) {
    return this._elements[name];
  }
}
