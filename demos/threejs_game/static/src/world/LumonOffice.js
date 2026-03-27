import * as THREE from 'three';
import { CONFIG } from '../config.js';
import { Materials } from './Materials.js';
import {
  createDeskIsland,
  createFilingCabinet,
  createWaterCooler,
  createDoubleDoor,
  createSideTable,
  createPottedPlant,
  createWallClock,
  createCoreValuesPoster,
  createPainting,
  createKeyboard,
} from './Furniture.js';

export class LumonOffice {
  constructor(scene) {
    this.scene = scene;
    this.collisionBoxes = [];

    /** @type {THREE.Mesh[]} Wall/ceiling/floor meshes for camera collision */
    this.wallMeshes = [];

    /** @type {Map<string, THREE.Group>} Named furniture references for interaction */
    this.interactables = new Map();

    /** @type {THREE.PointLight[]} Ceiling panel point lights */
    this.ceilingLights = [];

    this._buildRoom();
    this._buildWallPanels();
    this._buildCeilingLightPanels();
    this._buildFurniture();
    this._buildWallDecor();
    this._buildInteractableTriggers();
  }

  // --- Collision helper ---

  _addCollisionBox(x, z, w, d) {
    this.collisionBoxes.push({
      min: new THREE.Vector3(x - w / 2, 0, z - d / 2),
      max: new THREE.Vector3(x + w / 2, CONFIG.WALL_HEIGHT, z + d / 2),
    });
  }

  _addWall(x, y, z, w, h, d, material = Materials.wall) {
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), material);
    mesh.position.set(x, y, z);
    mesh.castShadow = true;
    mesh.receiveShadow = true;
    this.scene.add(mesh);
    this.wallMeshes.push(mesh);

    this.collisionBoxes.push({
      min: new THREE.Vector3(x - w / 2, 0, z - d / 2),
      max: new THREE.Vector3(x + w / 2, h, z + d / 2),
    });
  }

  // --- Room structure ---

  _buildRoom() {
    const W = CONFIG.ROOM_WIDTH;
    const D = CONFIG.ROOM_DEPTH;
    const H = CONFIG.WALL_HEIGHT;
    const T = CONFIG.WALL_THICKNESS;
    const halfW = W / 2;
    const halfD = D / 2;

    // Green carpet floor
    const floor = new THREE.Mesh(
      new THREE.BoxGeometry(W, 0.1, D),
      Materials.floor
    );
    floor.position.set(0, -0.05, 0);
    floor.receiveShadow = true;
    this.scene.add(floor);
    this.wallMeshes.push(floor);

    // Ceiling
    const ceiling = new THREE.Mesh(
      new THREE.BoxGeometry(W, 0.1, D),
      Materials.ceiling
    );
    ceiling.position.set(0, H + 0.05, 0);
    this.scene.add(ceiling);
    this.wallMeshes.push(ceiling);

    // Back wall (+Z)
    this._addWall(0, H / 2, halfD + T / 2, W + T * 2, H, T);

    // Front wall (-Z)
    this._addWall(0, H / 2, -halfD - T / 2, W + T * 2, H, T);

    // Left wall (-X)
    this._addWall(-halfW - T / 2, H / 2, 0, T, H, D);

    // Right wall (+X)
    this._addWall(halfW + T / 2, H / 2, 0, T, H, D);

    // Dark trim strips along top and bottom of all walls
    this._buildTrimStrips();
  }

  _buildTrimStrips() {
    const W = CONFIG.ROOM_WIDTH;
    const D = CONFIG.ROOM_DEPTH;
    const H = CONFIG.WALL_HEIGHT;
    const halfW = W / 2;
    const halfD = D / 2;
    const trimH = 0.06;
    const trimD = 0.02;

    // Top trim on each wall
    const topY = H - trimH / 2;
    const bottomY = trimH / 2;

    // Back and front walls
    for (const zSign of [1, -1]) {
      const z = zSign * (halfD + 0.01);
      for (const y of [topY, bottomY]) {
        const trim = new THREE.Mesh(
          new THREE.BoxGeometry(W, trimH, trimD),
          Materials.wallTrim
        );
        trim.position.set(0, y, z * (1 - 0.01));
        this.scene.add(trim);
      }
    }

    // Left and right walls
    for (const xSign of [-1, 1]) {
      const x = xSign * (halfW + 0.01);
      for (const y of [topY, bottomY]) {
        const trim = new THREE.Mesh(
          new THREE.BoxGeometry(trimD, trimH, D),
          Materials.wallTrim
        );
        trim.position.set(x * (1 - 0.01), y, 0);
        this.scene.add(trim);
      }
    }
  }

  _buildWallPanels() {
    const W = CONFIG.ROOM_WIDTH;
    const D = CONFIG.ROOM_DEPTH;
    const H = CONFIG.WALL_HEIGHT;
    const halfW = W / 2;
    const halfD = D / 2;
    const lineThick = 0.005;
    const lineDepth = 0.015;

    // Vertical panel lines on back wall (+Z)
    const panelSpacing = 2.0;
    for (let x = -halfW + panelSpacing; x < halfW; x += panelSpacing) {
      const line = new THREE.Mesh(
        new THREE.BoxGeometry(lineThick, H - 0.2, lineDepth),
        Materials.wallPanelLine
      );
      line.position.set(x, H / 2, halfD - 0.01);
      this.scene.add(line);
    }

    // Vertical panel lines on front wall (-Z)
    for (let x = -halfW + panelSpacing; x < halfW; x += panelSpacing) {
      const line = new THREE.Mesh(
        new THREE.BoxGeometry(lineThick, H - 0.2, lineDepth),
        Materials.wallPanelLine
      );
      line.position.set(x, H / 2, -halfD + 0.01);
      this.scene.add(line);
    }

    // Vertical panel lines on left wall (-X)
    for (let z = -halfD + panelSpacing; z < halfD; z += panelSpacing) {
      const line = new THREE.Mesh(
        new THREE.BoxGeometry(lineDepth, H - 0.2, lineThick),
        Materials.wallPanelLine
      );
      line.position.set(-halfW + 0.01, H / 2, z);
      this.scene.add(line);
    }

    // Vertical panel lines on right wall (+X)
    for (let z = -halfD + panelSpacing; z < halfD; z += panelSpacing) {
      const line = new THREE.Mesh(
        new THREE.BoxGeometry(lineDepth, H - 0.2, lineThick),
        Materials.wallPanelLine
      );
      line.position.set(halfW - 0.01, H / 2, z);
      this.scene.add(line);
    }
  }

  // --- Ceiling light panels ---

  _buildCeilingLightPanels() {
    const H = CONFIG.WALL_HEIGHT;

    // Large recessed light panels arranged in a grid pattern
    // Based on reference: geometric panels with bright white light
    const panels = [
      // Left column
      { x: -4.5, z: -2.5, w: 4, d: 3 },
      { x: -4.5, z: 2.5, w: 4, d: 3 },
      // Right column
      { x: 4.5, z: -2.5, w: 4, d: 3 },
      { x: 4.5, z: 2.5, w: 4, d: 3 },
      // Center strip
      { x: 0, z: 0, w: 3, d: 4 },
    ];

    for (const p of panels) {
      // Recessed panel (slightly below ceiling)
      const panel = new THREE.Mesh(
        new THREE.BoxGeometry(p.w, 0.08, p.d),
        Materials.ceilingLight
      );
      panel.position.set(p.x, H - 0.04, p.z);
      this.scene.add(panel);

      // Beveled frame around panel
      const frameW = 0.08;
      const frameMat = Materials.ceiling;

      // Frame edges (4 sides)
      const top = new THREE.Mesh(new THREE.BoxGeometry(p.w + frameW * 2, 0.12, frameW), frameMat);
      top.position.set(p.x, H - 0.06, p.z - p.d / 2 - frameW / 2);
      this.scene.add(top);

      const bottom = new THREE.Mesh(new THREE.BoxGeometry(p.w + frameW * 2, 0.12, frameW), frameMat);
      bottom.position.set(p.x, H - 0.06, p.z + p.d / 2 + frameW / 2);
      this.scene.add(bottom);

      const left = new THREE.Mesh(new THREE.BoxGeometry(frameW, 0.12, p.d), frameMat);
      left.position.set(p.x - p.w / 2 - frameW / 2, H - 0.06, p.z);
      this.scene.add(left);

      const right = new THREE.Mesh(new THREE.BoxGeometry(frameW, 0.12, p.d), frameMat);
      right.position.set(p.x + p.w / 2 + frameW / 2, H - 0.06, p.z);
      this.scene.add(right);

      // Area light substitute: point lights under each panel
      const light = new THREE.PointLight(0xfff8f0, 1.2, 12);
      light.position.set(p.x, H - 0.3, p.z);
      this.scene.add(light);
      this.ceilingLights.push(light);
    }

    // Diagonal ceiling beams (geometric pattern from reference)
    const beamMat = Materials.ceiling;
    const beamH = 0.15;
    const beamW = 0.12;

    // Cross beams connecting panels
    const beams = [
      // Horizontal dividers
      { x: 0, z: -1, w: CONFIG.ROOM_WIDTH - 1, d: beamW, h: beamH },
      { x: 0, z: 1, w: CONFIG.ROOM_WIDTH - 1, d: beamW, h: beamH },
      { x: 0, z: 4, w: CONFIG.ROOM_WIDTH - 1, d: beamW, h: beamH },
      { x: 0, z: -4, w: CONFIG.ROOM_WIDTH - 1, d: beamW, h: beamH },
      // Vertical dividers
      { x: -2.5, z: 0, w: beamW, d: CONFIG.ROOM_DEPTH - 1, h: beamH },
      { x: 2.5, z: 0, w: beamW, d: CONFIG.ROOM_DEPTH - 1, h: beamH },
      { x: -6.5, z: 0, w: beamW, d: CONFIG.ROOM_DEPTH - 1, h: beamH },
      { x: 6.5, z: 0, w: beamW, d: CONFIG.ROOM_DEPTH - 1, h: beamH },
    ];

    for (const b of beams) {
      const beam = new THREE.Mesh(
        new THREE.BoxGeometry(b.w, b.h, b.d),
        beamMat
      );
      beam.position.set(b.x, CONFIG.WALL_HEIGHT - b.h / 2, b.z);
      this.scene.add(beam);
    }
  }

  // --- Furniture placement ---

  _buildFurniture() {
    // Central 4-person desk island (centered in room)
    const deskIsland = createDeskIsland(0, 0);
    this.scene.add(deskIsland);
    // Collision: desk tops span ±1.65 X, chairs extend to ±2.3 Z
    this._addCollisionBox(0, 0, 4.0, 5.0);

    // Keyboard on Laurent's desk (back-left quadrant)
    const keyboard = createKeyboard(-0.9, 0.63, 1.0, 0);
    this.scene.add(keyboard);

    // Filing cabinets along left wall — one merged box covering all 3
    const cab1 = createFilingCabinet(-7.2, -1.5, Math.PI / 2);
    this.scene.add(cab1);
    const cab2 = createFilingCabinet(-7.2, -0.5, Math.PI / 2);
    this.scene.add(cab2);
    const cab3 = createFilingCabinet(-7.2, 0.5, Math.PI / 2);
    this.scene.add(cab3);
    // Merged: covers from z=-1.8 to z=0.8, width 0.6 (rotated cabinet depth)
    this._addCollisionBox(-7.2, -0.5, 0.7, 2.6);

    // Water cooler (left wall, behind filing cabinets)
    const cooler = createWaterCooler(-7.0, -3.0, Math.PI / 2);
    this.scene.add(cooler);
    this._addCollisionBox(-7.0, -3.0, 0.6, 0.6);

    // Green double door on back wall (centered) — block the doorway
    const door = createDoubleDoor(0, 5.85, 0);
    this.scene.add(door);
    this._addCollisionBox(0, 5.85, 2.0, 0.3);

    // Side table / credenza near right wall (rotated -90°, so 1.2 local-X → world-Z)
    const sideTable = createSideTable(6.5, 2.0, -Math.PI / 2);
    this.scene.add(sideTable);
    this._addCollisionBox(6.5, 2.0, 0.6, 1.4);

    // Potted plants — larger collision to cover foliage
    const plant1 = createPottedPlant(-6.5, 2.5);
    this.scene.add(plant1);
    this._addCollisionBox(-6.5, 2.5, 0.8, 0.8);

    const plant2 = createPottedPlant(6.5, -2.5);
    this.scene.add(plant2);
    this._addCollisionBox(6.5, -2.5, 0.8, 0.8);
  }

  // --- Wall decorations ---

  _buildWallDecor() {
    const halfD = CONFIG.ROOM_DEPTH / 2;

    // Wall clock on back wall (above door)
    const clock = createWallClock(0, 2.6, halfD - 0.01, Math.PI);
    this.scene.add(clock);

    // Core Values poster on left wall
    const poster = createCoreValuesPoster(
      -CONFIG.ROOM_WIDTH / 2 + 0.02, 1.6, -3.5, Math.PI / 2
    );
    this.scene.add(poster);

    // Kier Eagan portrait on front wall (-Z), facing +Z
    const painting = createPainting(3.0, 1.8, -halfD + 0.03, 0);
    this.scene.add(painting);
  }

  // --- Interactable trigger zones ---

  _buildInteractableTriggers() {
    const triggerMat = new THREE.MeshBasicMaterial({
      visible: false,
    });

    const makeTrigger = (name, x, y, z, w, h, d) => {
      const mesh = new THREE.Mesh(
        new THREE.BoxGeometry(w, h, d),
        triggerMat
      );
      mesh.position.set(x, y, z);
      this.scene.add(mesh);
      this.interactables.set(name, mesh);
      return mesh;
    };

    // Desk terminal — the CRT monitor area on Mark's desk (front-left quadrant)
    makeTrigger('terminal', -0.9, 1.0, -0.3, 0.6, 0.5, 0.5);

    // Filing cabinet — middle cabinet along left wall
    makeTrigger('filing_cabinet', -7.2, 0.6, -0.5, 0.7, 1.2, 0.7);

    // Door keypad — right side of the double door
    makeTrigger('keypad', 1.15, 1.2, 5.85, 0.3, 0.4, 0.3);

    // Water cooler
    makeTrigger('water_cooler', -7.0, 0.7, -3.0, 0.6, 1.4, 0.6);

    // Clue: strange note on the floor near the side table (visual only)
    const noteMat = new THREE.MeshStandardMaterial({ color: 0xf5f0e0, roughness: 0.9 });
    const noteGeo = new THREE.BoxGeometry(0.15, 0.01, 0.1);
    const noteVis = new THREE.Mesh(noteGeo, noteMat);
    noteVis.position.set(6.2, 0.01, 2.8);
    noteVis.rotation.y = 0.3;
    noteVis.castShadow = true;
    this.scene.add(noteVis);

    // Clue trigger: Kier's Portrait (painting on front wall)
    makeTrigger('painting', 3.0, 1.8, -CONFIG.ROOM_DEPTH / 2 + 0.25, 1.2, 1.0, 0.5);

    // Clue: Ricken's Book — small maroon book near the water cooler (left wall)
    const bookGroup = new THREE.Group();
    bookGroup.position.set(-6.5, 0.01, -3.0);
    bookGroup.rotation.y = 0.4;

    // Book cover (maroon)
    const bookCover = new THREE.Mesh(
      new THREE.BoxGeometry(0.18, 0.04, 0.12),
      new THREE.MeshStandardMaterial({ color: 0x5c1a1a, roughness: 0.7 })
    );
    bookGroup.add(bookCover);

    // Cream page edge (visible from the side)
    const pageEdge = new THREE.Mesh(
      new THREE.BoxGeometry(0.16, 0.03, 0.005),
      new THREE.MeshStandardMaterial({ color: 0xf5f0dc, roughness: 0.9 })
    );
    pageEdge.position.set(0, 0, 0.062);
    bookGroup.add(pageEdge);

    bookGroup.castShadow = true;
    this.scene.add(bookGroup);

    // Clue trigger: Ricken's Book (near water cooler)
    makeTrigger('book', -6.5, 0.1, -3.0, 0.3, 0.2, 0.25);
  }
}
