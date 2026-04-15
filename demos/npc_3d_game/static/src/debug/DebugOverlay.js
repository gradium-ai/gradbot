import * as THREE from 'three';

/**
 * Debug visualization for collision boxes, proximity triggers, and interaction triggers.
 *
 * Enable: ?debug=1 URL param (auto-shows on load) or F3 key toggle.
 *
 * Shows:
 *  - Red wireframes: collision boxes (walls, furniture)
 *  - Green wireframe cylinders: proximity triggers (clue zones)
 *  - Yellow wireframes: raycast interaction triggers
 *  - Cyan wireframe circle: player collision radius
 *
 * Edit mode (F4 to toggle while debug is visible):
 *  - Exits pointer lock so you can use the mouse freely
 *  - Click a wireframe to select it (highlighted white)
 *  - Arrow keys to nudge selected object on XZ plane (hold Shift for fine)
 *  - Position changes are applied to the underlying data (collision box / trigger)
 *  - Console logs the new position so you can copy it back to code
 */
export class DebugOverlay {
  /**
   * @param {THREE.Scene} scene
   * @param {THREE.Camera} camera
   * @param {HTMLCanvasElement} canvas
   * @param {object} opts
   * @param {Array} opts.collisionBoxes
   * @param {import('../game/InteractionSystem.js').InteractionSystem} opts.interactionSystem
   * @param {THREE.Object3D} opts.playerModel
   * @param {number} opts.playerRadius
   */
  constructor(scene, camera, canvas, { collisionBoxes, interactionSystem, playerModel, playerRadius }) {
    this.scene = scene;
    this.camera = camera;
    this.canvas = canvas;
    this._collisionBoxes = collisionBoxes;
    this._interactionSystem = interactionSystem;
    this._playerModel = playerModel;
    this._playerRadius = playerRadius;

    this._visible = false;
    this._editMode = false;
    this._meshes = [];           // all debug meshes (for show/hide)
    this._selectables = [];      // {mesh, type, source, label?} — things you can click & move
    this._selected = null;       // currently selected selectable entry
    this._playerCircle = null;
    this._hudEl = null;

    this._raycaster = new THREE.Raycaster();
    this._mouse = new THREE.Vector2();

    this._build();
    this._buildHUD();
    this._setupInput();

    // Auto-show if ?debug=1 (dev builds only)
    const autoShow = !!(import.meta.env?.DEV && new URLSearchParams(window.location.search).get('debug') === '1');
    this.setVisible(autoShow);
  }

  // ── Public ──────────────────────────────────────────────

  get visible() { return this._visible; }

  setVisible(v) {
    this._visible = v;
    for (const m of this._meshes) m.visible = v;
    if (this._playerCircle) this._playerCircle.visible = v;
    if (this._hudEl) this._hudEl.style.display = v ? 'block' : 'none';
    if (!v) this._setEditMode(false);
  }

  toggle() { this.setVisible(!this._visible); }

  update() {
    if (!this._visible) return;
    if (this._playerCircle && this._playerModel) {
      this._playerCircle.position.set(
        this._playerModel.position.x, 0.05, this._playerModel.position.z,
      );
    }
    this._updateHUD();
  }

  rebuild() {
    const wasVisible = this._visible;
    this._deselect();
    this._clear();
    this._build();
    this.setVisible(wasVisible);
  }

  // ── Edit mode ─────────────────────────────────────────

  _setEditMode(on) {
    if (on === this._editMode) return;
    this._editMode = on;
    if (on) {
      // Exit pointer lock so user can click freely
      if (document.pointerLockElement) document.exitPointerLock();
    }
    if (!on) this._deselect();
  }

  _deselect() {
    if (this._selected) {
      this._selected.mesh.material.color.copy(this._selected._origColor);
      this._selected = null;
    }
  }

  _selectEntry(entry) {
    this._deselect();
    this._selected = entry;
    // Store original color, highlight white
    entry._origColor = entry.mesh.material.color.clone();
    entry.mesh.material.color.set(0xffffff);
  }

  _nudge(dx, dz) {
    const entry = this._selected;
    if (!entry) return;

    if (entry.type === 'collision') {
      const box = entry.source;
      box.min.x += dx;
      box.max.x += dx;
      box.min.z += dz;
      box.max.z += dz;
      entry.mesh.position.x += dx;
      entry.mesh.position.z += dz;
      if (entry.label) { entry.label.position.x += dx; entry.label.position.z += dz; }
      const cx = ((box.min.x + box.max.x) / 2).toFixed(2);
      const cz = ((box.min.z + box.max.z) / 2).toFixed(2);
      const w = (box.max.x - box.min.x).toFixed(2);
      const d = (box.max.z - box.min.z).toFixed(2);
      console.log(`[Debug] collision box → center(${cx}, ${cz})  size(${w} x ${d})`);
    }

    if (entry.type === 'proximity') {
      const trigger = entry.source;
      trigger.position.x += dx;
      trigger.position.z += dz;
      // Move all associated meshes
      for (const m of entry.linkedMeshes) {
        m.position.x += dx;
        m.position.z += dz;
      }
      console.log(`[Debug] proximity "${trigger.data.name}" → (${trigger.position.x.toFixed(2)}, ${trigger.position.z.toFixed(2)})`);
    }

    if (entry.type === 'interactable') {
      const obj = entry.source;
      obj.position.x += dx;
      obj.position.z += dz;
      entry.mesh.position.x += dx;
      entry.mesh.position.z += dz;
      if (entry.label) { entry.label.position.x += dx; entry.label.position.z += dz; }
      console.log(`[Debug] interactable "${entry.name}" → (${obj.position.x.toFixed(2)}, ${obj.position.y.toFixed(2)}, ${obj.position.z.toFixed(2)})`);
    }
  }

  // ── Build ─────────────────────────────────────────────

  _clear() {
    for (const m of this._meshes) {
      this.scene.remove(m);
      if (m.geometry) m.geometry.dispose();
    }
    this._meshes = [];
    this._selectables = [];
    if (this._playerCircle) {
      this.scene.remove(this._playerCircle);
      this._playerCircle.geometry.dispose();
      this._playerCircle = null;
    }
  }

  _build() {
    // 1. Collision boxes — red wireframe
    for (const box of this._collisionBoxes) {
      const size = new THREE.Vector3().subVectors(box.max, box.min);
      const center = new THREE.Vector3().addVectors(box.max, box.min).multiplyScalar(0.5);

      const geo = new THREE.BoxGeometry(size.x, size.y, size.z);
      const edges = new THREE.EdgesGeometry(geo);
      const mat = new THREE.LineBasicMaterial({ color: 0xff3333 });
      const line = new THREE.LineSegments(edges, mat);
      line.position.copy(center);
      line.renderOrder = 999;
      this.scene.add(line);
      this._meshes.push(line);

      // Invisible clickable mesh for selection
      const clickMat = new THREE.MeshBasicMaterial({ visible: false, side: THREE.DoubleSide });
      const clickMesh = new THREE.Mesh(new THREE.BoxGeometry(size.x, size.y, size.z), clickMat);
      clickMesh.position.copy(center);
      this.scene.add(clickMesh);
      this._meshes.push(clickMesh);

      this._selectables.push({ mesh: line, clickTarget: clickMesh, type: 'collision', source: box, label: null });
      geo.dispose();
    }

    // 2. Proximity triggers — green wireframe cylinders
    const proxTriggers = this._interactionSystem._proximityTriggers;
    for (const trigger of proxTriggers) {
      const linkedMeshes = [];

      // Bottom ring
      const ringGeo = new THREE.CylinderGeometry(trigger.radius, trigger.radius, 0.1, 32);
      const ringEdges = new THREE.EdgesGeometry(ringGeo);
      const ringMat = new THREE.LineBasicMaterial({ color: 0x33ff66 });
      const ring = new THREE.LineSegments(ringEdges, ringMat);
      ring.position.set(trigger.position.x, 0.05, trigger.position.z);
      ring.renderOrder = 999;
      this.scene.add(ring);
      this._meshes.push(ring);
      linkedMeshes.push(ring);
      ringGeo.dispose();

      // Vertical pillar
      const pillarGeo = new THREE.CylinderGeometry(trigger.radius, trigger.radius, 2.0, 32, 1, true);
      const pillarEdges = new THREE.EdgesGeometry(pillarGeo);
      const pillarMat = new THREE.LineBasicMaterial({ color: 0x33ff66 });
      const pillar = new THREE.LineSegments(pillarEdges, pillarMat);
      pillar.position.set(trigger.position.x, 1.0, trigger.position.z);
      pillar.renderOrder = 999;
      this.scene.add(pillar);
      this._meshes.push(pillar);
      linkedMeshes.push(pillar);
      pillarGeo.dispose();

      // Label
      const label = this._addLabel(trigger.data.name, trigger.position.x, 2.2, trigger.position.z, 0x33ff66);
      linkedMeshes.push(label);

      // Invisible click target (cylinder)
      const clickGeo = new THREE.CylinderGeometry(trigger.radius, trigger.radius, 2.0, 16);
      const clickMat = new THREE.MeshBasicMaterial({ visible: false });
      const clickMesh = new THREE.Mesh(clickGeo, clickMat);
      clickMesh.position.set(trigger.position.x, 1.0, trigger.position.z);
      this.scene.add(clickMesh);
      this._meshes.push(clickMesh);
      linkedMeshes.push(clickMesh);

      this._selectables.push({
        mesh: ring, clickTarget: clickMesh, type: 'proximity',
        source: trigger, linkedMeshes, label,
      });
    }

    // 3. Raycast interaction triggers — yellow wireframe
    const interactables = this._interactionSystem._interactables;
    for (const [obj, data] of interactables) {
      const bbox = new THREE.Box3().setFromObject(obj);
      const size = new THREE.Vector3();
      const center = new THREE.Vector3();
      bbox.getSize(size);
      bbox.getCenter(center);

      const geo = new THREE.BoxGeometry(size.x, size.y, size.z);
      const edges = new THREE.EdgesGeometry(geo);
      const mat = new THREE.LineBasicMaterial({ color: 0xffcc00 });
      const line = new THREE.LineSegments(edges, mat);
      line.position.copy(center);
      line.renderOrder = 999;
      this.scene.add(line);
      this._meshes.push(line);
      geo.dispose();

      const label = this._addLabel(data.name, center.x, center.y + size.y / 2 + 0.2, center.z, 0xffcc00);

      // Click target
      const clickMat = new THREE.MeshBasicMaterial({ visible: false });
      const clickMesh = new THREE.Mesh(new THREE.BoxGeometry(size.x, size.y, size.z), clickMat);
      clickMesh.position.copy(center);
      this.scene.add(clickMesh);
      this._meshes.push(clickMesh);

      this._selectables.push({
        mesh: line, clickTarget: clickMesh, type: 'interactable',
        source: obj, name: data.name, label,
      });
    }

    // 4. Player collision radius — cyan circle
    const cyanMat = new THREE.LineBasicMaterial({ color: 0x00ccff });
    const circleGeo = new THREE.RingGeometry(this._playerRadius - 0.01, this._playerRadius, 32);
    const circleEdges = new THREE.EdgesGeometry(circleGeo);
    this._playerCircle = new THREE.LineSegments(circleEdges, cyanMat);
    this._playerCircle.rotation.x = -Math.PI / 2;
    this._playerCircle.renderOrder = 999;
    this.scene.add(this._playerCircle);
    circleGeo.dispose();
  }

  _addLabel(text, x, y, z, color) {
    const canvas = document.createElement('canvas');
    canvas.width = 256;
    canvas.height = 64;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'rgba(0,0,0,0.6)';
    ctx.fillRect(0, 0, 256, 64);
    ctx.font = 'bold 20px monospace';
    ctx.fillStyle = '#' + color.toString(16).padStart(6, '0');
    ctx.textAlign = 'center';
    ctx.textBaseline = 'middle';
    ctx.fillText(text, 128, 32);

    const tex = new THREE.CanvasTexture(canvas);
    const spriteMat = new THREE.SpriteMaterial({ map: tex, depthTest: false });
    const sprite = new THREE.Sprite(spriteMat);
    sprite.position.set(x, y, z);
    sprite.scale.set(1.5, 0.4, 1);
    sprite.renderOrder = 1000;
    this.scene.add(sprite);
    this._meshes.push(sprite);
    return sprite;
  }

  // ── HUD ───────────────────────────────────────────────

  _buildHUD() {
    this._hudEl = document.createElement('div');
    this._hudEl.style.cssText = `
      position: fixed; top: 8px; left: 8px;
      padding: 8px 12px;
      background: rgba(0, 0, 0, 0.8);
      color: #00ff88;
      font-family: 'Courier New', monospace;
      font-size: 12px;
      line-height: 1.6;
      border: 1px solid rgba(0, 255, 136, 0.3);
      pointer-events: none;
      z-index: 200;
      white-space: pre;
    `;
    document.body.appendChild(this._hudEl);
  }

  _updateHUD() {
    if (!this._hudEl || !this._playerModel) return;
    const p = this._playerModel.position;
    const rot = (this._playerModel.rotation.y * 180 / Math.PI).toFixed(1);
    const lines = [
      `DEBUG  F3:toggle  F4:edit mode`,
      `pos: (${p.x.toFixed(2)}, ${p.z.toFixed(2)})  rot: ${rot}°`,
      `boxes: ${this._collisionBoxes.length}  prox: ${this._interactionSystem._proximityTriggers.length}  ray: ${this._interactionSystem._interactables.size}`,
    ];
    if (this._editMode) {
      lines.push(`── EDIT MODE ──`);
      lines.push(`click to select | arrows to move | shift=fine`);
      if (this._selected) {
        const t = this._selected.type;
        const n = this._selected.name || this._selected.source?.data?.name || 'collision box';
        lines.push(`selected: [${t}] ${n}`);
      }
    }
    this._hudEl.textContent = lines.join('\n');
  }

  // ── Input ─────────────────────────────────────────────

  _setupInput() {
    window.addEventListener('keydown', (e) => {
      if (e.code === 'F3') {
        e.preventDefault();
        this.toggle();
        return;
      }
      if (e.code === 'F4' && this._visible) {
        e.preventDefault();
        this._setEditMode(!this._editMode);
        return;
      }

      // Arrow key nudging in edit mode
      if (this._editMode && this._selected) {
        const step = e.shiftKey ? 0.05 : 0.25;
        if (e.code === 'ArrowUp')    { e.preventDefault(); this._nudge(0, -step); }
        if (e.code === 'ArrowDown')  { e.preventDefault(); this._nudge(0, step); }
        if (e.code === 'ArrowLeft')  { e.preventDefault(); this._nudge(-step, 0); }
        if (e.code === 'ArrowRight') { e.preventDefault(); this._nudge(step, 0); }
      }
    });

    // Click to select in edit mode
    this.canvas.addEventListener('click', (e) => {
      if (!this._visible || !this._editMode) return;
      // Don't pick if pointer locked (game mode)
      if (document.pointerLockElement) return;

      const rect = this.canvas.getBoundingClientRect();
      this._mouse.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      this._mouse.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

      this._raycaster.setFromCamera(this._mouse, this.camera);

      const clickTargets = this._selectables.map(s => s.clickTarget);
      const hits = this._raycaster.intersectObjects(clickTargets, false);

      if (hits.length > 0) {
        const hitMesh = hits[0].object;
        const entry = this._selectables.find(s => s.clickTarget === hitMesh);
        if (entry) this._selectEntry(entry);
      } else {
        this._deselect();
      }
    });
  }
}
