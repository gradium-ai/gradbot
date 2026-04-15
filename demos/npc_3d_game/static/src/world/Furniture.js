import * as THREE from 'three';
import { Materials } from './Materials.js';

/**
 * Creates a 4-person desk island with green partitions and CRT monitors.
 * Desks face outward from center, partitions form a cross in the middle.
 */
export function createDeskIsland(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);

  // Central green partition cross
  const partH = 0.5;
  const partBase = 0.62; // above desk height

  // Vertical partition (along Z)
  const partV = new THREE.Mesh(
    new THREE.BoxGeometry(0.06, partH, 3.6),
    Materials.deskPartition
  );
  partV.position.y = partBase + partH / 2;
  partV.castShadow = true;
  group.add(partV);

  // Horizontal partition (along X)
  const partHz = new THREE.Mesh(
    new THREE.BoxGeometry(3.6, partH, 0.06),
    Materials.deskPartition
  );
  partHz.position.y = partBase + partH / 2;
  partHz.castShadow = true;
  group.add(partHz);

  // 4 desks, one per quadrant
  const quads = [
    { dx: -0.9, dz: -0.9, ry: 0 },         // front-left, faces -Z
    { dx: 0.9, dz: -0.9, ry: Math.PI },    // front-right, faces +Z
    { dx: -0.9, dz: 0.9, ry: 0 },          // back-left, faces -Z
    { dx: 0.9, dz: 0.9, ry: Math.PI },     // back-right, faces +Z
  ];

  for (const q of quads) {
    // Desk surface (cream colored)
    const deskTop = new THREE.Mesh(
      new THREE.BoxGeometry(1.5, 0.06, 1.4),
      Materials.desk
    );
    deskTop.position.set(q.dx, 0.60, q.dz);
    deskTop.receiveShadow = true;
    deskTop.castShadow = true;
    group.add(deskTop);

    // Desk pedestal/base (dark)
    const deskPed = new THREE.Mesh(
      new THREE.BoxGeometry(0.5, 0.58, 0.6),
      Materials.deskBase
    );
    deskPed.position.set(q.dx + (q.ry === 0 ? 0.4 : -0.4), 0.29, q.dz);
    group.add(deskPed);

    // Desk front panel
    const frontPanel = new THREE.Mesh(
      new THREE.BoxGeometry(1.5, 0.55, 0.04),
      Materials.desk
    );
    const fz = q.dz + (q.ry === 0 ? 0.68 : -0.68);
    frontPanel.position.set(q.dx, 0.31, fz);
    group.add(frontPanel);

    // CRT Monitor — chunky retro box
    const crt = new THREE.Mesh(
      new THREE.BoxGeometry(0.45, 0.38, 0.35),
      Materials.monitor
    );
    const monZ = q.dz + (q.ry === 0 ? -0.3 : 0.3);
    crt.position.set(q.dx, 0.82, monZ);
    crt.rotation.y = q.ry;
    crt.castShadow = true;
    group.add(crt);

    // Green screen face
    const screen = new THREE.Mesh(
      new THREE.BoxGeometry(0.38, 0.30, 0.02),
      Materials.monitorScreen
    );
    const scrZ = q.dz + (q.ry === 0 ? -0.12 : 0.12);
    screen.position.set(q.dx, 0.82, scrZ);
    screen.rotation.y = q.ry;
    group.add(screen);

    // Chair (dark, on circular mat)
    const chairMat = new THREE.Mesh(
      new THREE.CylinderGeometry(0.35, 0.35, 0.02, 16),
      Materials.chairMat
    );
    const chairZ = q.dz + (q.ry === 0 ? 1.1 : -1.1);
    chairMat.position.set(q.dx, 0.01, chairZ);
    group.add(chairMat);

    // Chair base (5-star base simplified as cylinder)
    const chairBase = new THREE.Mesh(
      new THREE.CylinderGeometry(0.2, 0.25, 0.08, 8),
      Materials.chair
    );
    chairBase.position.set(q.dx, 0.12, chairZ);
    group.add(chairBase);

    // Chair seat
    const seat = new THREE.Mesh(
      new THREE.BoxGeometry(0.45, 0.06, 0.42),
      Materials.chair
    );
    seat.position.set(q.dx, 0.34, chairZ);
    group.add(seat);

    // Chair backrest
    const backrest = new THREE.Mesh(
      new THREE.BoxGeometry(0.45, 0.4, 0.06),
      Materials.chair
    );
    const backZ = chairZ + (q.ry === 0 ? 0.2 : -0.2);
    backrest.position.set(q.dx, 0.55, backZ);
    group.add(backrest);
  }

  return group;
}

/**
 * Creates a filing cabinet (3-drawer style).
 */
export function createFilingCabinet(x, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  group.rotation.y = rotY;

  const body = new THREE.Mesh(
    new THREE.BoxGeometry(0.5, 1.2, 0.55),
    Materials.filingCabinet
  );
  body.position.y = 0.6;
  body.castShadow = true;
  body.receiveShadow = true;
  group.add(body);

  // Drawer handles (3 drawers)
  const handleMat = new THREE.MeshStandardMaterial({ color: 0x555555 });
  for (let i = 0; i < 3; i++) {
    const handle = new THREE.Mesh(
      new THREE.BoxGeometry(0.2, 0.02, 0.02),
      handleMat
    );
    handle.position.set(0, 0.3 + i * 0.35, -0.28);
    group.add(handle);

    // Drawer line
    const line = new THREE.Mesh(
      new THREE.BoxGeometry(0.48, 0.01, 0.01),
      handleMat
    );
    line.position.set(0, 0.15 + i * 0.35, -0.28);
    group.add(line);
  }

  return group;
}

/**
 * Creates a water cooler.
 */
export function createWaterCooler(x, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  group.rotation.y = rotY;

  // Base/body
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(0.4, 0.9, 0.4),
    Materials.waterCooler
  );
  body.position.y = 0.45;
  body.castShadow = true;
  group.add(body);

  // Water bottle on top (blue jug)
  const bottle = new THREE.Mesh(
    new THREE.CylinderGeometry(0.14, 0.14, 0.5, 12),
    Materials.waterBottle
  );
  bottle.position.y = 1.15;
  group.add(bottle);

  // Bottle cap/neck
  const neck = new THREE.Mesh(
    new THREE.CylinderGeometry(0.06, 0.14, 0.1, 12),
    Materials.waterBottle
  );
  neck.position.y = 0.88;
  group.add(neck);

  // Spigot area
  const spigot = new THREE.Mesh(
    new THREE.BoxGeometry(0.08, 0.05, 0.06),
    new THREE.MeshStandardMaterial({ color: 0xdddddd })
  );
  spigot.position.set(0, 0.7, -0.22);
  group.add(spigot);

  return group;
}

/**
 * Creates a green double door with keypad on the back wall.
 */
export function createDoubleDoor(x, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  group.rotation.y = rotY;

  const doorH = 2.4;
  const doorW = 0.9;
  const gap = 0.02;

  // Left door panel
  const leftDoor = new THREE.Mesh(
    new THREE.BoxGeometry(doorW, doorH, 0.08),
    Materials.door
  );
  leftDoor.position.set(-doorW / 2 - gap, doorH / 2, 0);
  leftDoor.castShadow = true;
  group.add(leftDoor);

  // Right door panel
  const rightDoor = new THREE.Mesh(
    new THREE.BoxGeometry(doorW, doorH, 0.08),
    Materials.door
  );
  rightDoor.position.set(doorW / 2 + gap, doorH / 2, 0);
  rightDoor.castShadow = true;
  group.add(rightDoor);

  // Door frame (top)
  const frameTop = new THREE.Mesh(
    new THREE.BoxGeometry(doorW * 2 + 0.2, 0.1, 0.12),
    Materials.wallTrim
  );
  frameTop.position.set(0, doorH + 0.05, 0);
  group.add(frameTop);

  // Keypad (right side of door)
  const keypadBody = new THREE.Mesh(
    new THREE.BoxGeometry(0.12, 0.18, 0.04),
    Materials.keypad
  );
  keypadBody.position.set(doorW + 0.25, 1.2, -0.05);
  group.add(keypadBody);

  // Keypad screen
  const keypadScreen = new THREE.Mesh(
    new THREE.BoxGeometry(0.08, 0.05, 0.01),
    new THREE.MeshStandardMaterial({ color: 0x224422, emissive: 0x224422, emissiveIntensity: 0.5 })
  );
  keypadScreen.position.set(doorW + 0.25, 1.28, -0.07);
  group.add(keypadScreen);

  return group;
}

/**
 * Creates a small wooden side table / credenza.
 */
export function createSideTable(x, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);
  group.rotation.y = rotY;

  // Table body (credenza style)
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(1.2, 0.6, 0.5),
    Materials.sideTable
  );
  body.position.y = 0.3;
  body.castShadow = true;
  body.receiveShadow = true;
  group.add(body);

  // Legs (short)
  const legGeo = new THREE.BoxGeometry(0.06, 0.1, 0.06);
  const legMat = Materials.deskBase;
  const legPositions = [
    [-0.55, 0.05, -0.2],
    [-0.55, 0.05, 0.2],
    [0.55, 0.05, -0.2],
    [0.55, 0.05, 0.2],
  ];
  for (const [lx, ly, lz] of legPositions) {
    const leg = new THREE.Mesh(legGeo, legMat);
    leg.position.set(lx, ly, lz);
    group.add(leg);
  }

  // Small lamp on table
  const lampBase = new THREE.Mesh(
    new THREE.CylinderGeometry(0.06, 0.08, 0.04, 8),
    Materials.deskBase
  );
  lampBase.position.set(0.35, 0.62, 0);
  group.add(lampBase);

  const lampShade = new THREE.Mesh(
    new THREE.ConeGeometry(0.1, 0.15, 8),
    new THREE.MeshStandardMaterial({ color: 0xeeddcc })
  );
  lampShade.position.set(0.35, 0.78, 0);
  group.add(lampShade);

  return group;
}

/**
 * Creates a potted indoor tree/plant.
 */
export function createPottedPlant(x, z) {
  const group = new THREE.Group();
  group.position.set(x, 0, z);

  // Pot
  const pot = new THREE.Mesh(
    new THREE.CylinderGeometry(0.25, 0.2, 0.4, 12),
    Materials.plantPot
  );
  pot.position.y = 0.2;
  pot.castShadow = true;
  group.add(pot);

  // Soil
  const soil = new THREE.Mesh(
    new THREE.CylinderGeometry(0.23, 0.23, 0.04, 12),
    new THREE.MeshStandardMaterial({ color: 0x3a2a1a })
  );
  soil.position.y = 0.4;
  group.add(soil);

  // Trunk
  const trunk = new THREE.Mesh(
    new THREE.CylinderGeometry(0.04, 0.06, 0.8, 6),
    Materials.plantTrunk
  );
  trunk.position.y = 0.8;
  trunk.castShadow = true;
  group.add(trunk);

  // Foliage (cluster of spheres for tree canopy)
  const foliagePositions = [
    [0, 1.4, 0, 0.35],
    [-0.15, 1.55, 0.1, 0.25],
    [0.15, 1.5, -0.1, 0.28],
    [0, 1.65, 0, 0.22],
    [-0.1, 1.3, -0.15, 0.2],
    [0.12, 1.35, 0.12, 0.22],
  ];

  for (const [fx, fy, fz, fr] of foliagePositions) {
    const leaf = new THREE.Mesh(
      new THREE.SphereGeometry(fr, 8, 6),
      Materials.plantLeaves
    );
    leaf.position.set(fx, fy, fz);
    leaf.castShadow = true;
    group.add(leaf);
  }

  return group;
}

/**
 * Creates a wall clock.
 */
export function createWallClock(x, y, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, y, z);
  group.rotation.y = rotY;

  // Clock face
  const face = new THREE.Mesh(
    new THREE.CylinderGeometry(0.3, 0.3, 0.04, 24),
    Materials.clockFace
  );
  face.rotation.x = Math.PI / 2;
  group.add(face);

  // Rim
  const rim = new THREE.Mesh(
    new THREE.TorusGeometry(0.3, 0.02, 8, 24),
    Materials.clockRim
  );
  rim.rotation.x = Math.PI / 2;
  group.add(rim);

  // Hour hand
  const hourHand = new THREE.Mesh(
    new THREE.BoxGeometry(0.02, 0.15, 0.01),
    Materials.clockRim
  );
  hourHand.position.set(0, 0.06, -0.02);
  hourHand.rotation.z = -0.5; // ~10 o'clock
  group.add(hourHand);

  // Minute hand
  const minHand = new THREE.Mesh(
    new THREE.BoxGeometry(0.015, 0.22, 0.01),
    Materials.clockRim
  );
  minHand.position.set(0.06, 0.05, -0.02);
  minHand.rotation.z = -1.2;
  group.add(minHand);

  return group;
}

/**
 * Creates a "CORE VALUES" poster for the wall.
 */
/**
 * Creates a portrait painting of Kier Eagan for the wall.
 */
export function createPainting(x, y, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, y, z);
  group.rotation.y = rotY;

  // Dark frame
  const frame = new THREE.Mesh(
    new THREE.BoxGeometry(1.0, 0.8, 0.04),
    new THREE.MeshStandardMaterial({ color: 0x1a1008, roughness: 0.6 })
  );
  group.add(frame);

  // Canvas with Kier painting texture — plane faces +Z (into the room)
  const texture = new THREE.TextureLoader().load('assets/textures/kier_painting.png');
  texture.colorSpace = THREE.SRGBColorSpace;
  const canvas = new THREE.Mesh(
    new THREE.PlaneGeometry(0.88, 0.68),
    new THREE.MeshStandardMaterial({ map: texture, roughness: 0.85 })
  );
  canvas.position.z = 0.025;
  group.add(canvas);

  // Gold name plaque
  const plaque = new THREE.Mesh(
    new THREE.BoxGeometry(0.4, 0.06, 0.008),
    new THREE.MeshStandardMaterial({ color: 0xc5a54e, metalness: 0.6, roughness: 0.3 })
  );
  plaque.position.set(0, -0.28, -0.025);
  group.add(plaque);

  return group;
}

/**
 * Creates a retro keyboard (flat rectangular body with rows of raised keys).
 * Matches the Severance-era beige office aesthetic.
 */
export function createKeyboard(x, y, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, y, z);
  group.rotation.y = rotY;

  // Keyboard body — slightly angled beige slab
  const bodyW = 0.40;
  const bodyD = 0.15;
  const bodyH = 0.018;
  const body = new THREE.Mesh(
    new THREE.BoxGeometry(bodyW, bodyH, bodyD),
    new THREE.MeshStandardMaterial({ color: 0xd8d0c0, roughness: 0.8 })
  );
  group.add(body);

  // Slight upward tilt (back higher than front, like a real keyboard)
  group.rotation.x = -0.08;

  // Key grid — 5 rows of keys
  const keyMat = new THREE.MeshStandardMaterial({ color: 0xc8c0b0, roughness: 0.7 });
  const keyH = 0.01;
  const keyGap = 0.004;

  const rows = [
    { count: 12, keyW: 0.028, z: -0.055 },   // top row (number keys)
    { count: 11, keyW: 0.028, z: -0.025 },   // QWERTY row
    { count: 10, keyW: 0.028, z: 0.005 },    // home row
    { count: 9,  keyW: 0.028, z: 0.035 },    // bottom row
    { count: 1,  keyW: 0.18,  z: 0.06 },     // spacebar
  ];

  for (const row of rows) {
    const totalW = row.count * row.keyW + (row.count - 1) * keyGap;
    const startX = -totalW / 2 + row.keyW / 2;
    for (let i = 0; i < row.count; i++) {
      const key = new THREE.Mesh(
        new THREE.BoxGeometry(row.keyW, keyH, 0.022),
        keyMat
      );
      key.position.set(startX + i * (row.keyW + keyGap), bodyH / 2 + keyH / 2, row.z);
      group.add(key);
    }
  }

  return group;
}

export function createCoreValuesPoster(x, y, z, rotY = 0) {
  const group = new THREE.Group();
  group.position.set(x, y, z);
  group.rotation.y = rotY;

  // Frame
  const frame = new THREE.Mesh(
    new THREE.BoxGeometry(0.8, 1.0, 0.04),
    Materials.posterBg
  );
  group.add(frame);

  // Inner poster area
  const inner = new THREE.Mesh(
    new THREE.BoxGeometry(0.7, 0.9, 0.01),
    new THREE.MeshStandardMaterial({ color: 0xf0f0e8 })
  );
  inner.position.z = -0.02;
  group.add(inner);

  // "CORE VALUES" text block (green rectangle)
  const textBlock = new THREE.Mesh(
    new THREE.BoxGeometry(0.5, 0.15, 0.01),
    Materials.posterText
  );
  textBlock.position.set(0, 0.25, -0.03);
  group.add(textBlock);

  // Text lines (decorative)
  for (let i = 0; i < 4; i++) {
    const line = new THREE.Mesh(
      new THREE.BoxGeometry(0.45, 0.02, 0.005),
      new THREE.MeshStandardMaterial({ color: 0x888888 })
    );
    line.position.set(0, 0.05 - i * 0.1, -0.03);
    group.add(line);
  }

  return group;
}
