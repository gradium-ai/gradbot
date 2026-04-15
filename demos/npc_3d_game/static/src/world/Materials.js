import * as THREE from 'three';
import { CONFIG } from '../config.js';

export const Materials = {
  // Walls & structure
  wall: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_WALL, side: THREE.DoubleSide }),
  wallTrim: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_WALL_TRIM, side: THREE.DoubleSide }),
  wallPanelLine: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_WALL_PANEL_LINE, side: THREE.DoubleSide }),
  ceiling: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_CEILING, side: THREE.DoubleSide }),
  ceilingLight: new THREE.MeshStandardMaterial({
    color: CONFIG.COLOR_CEILING_LIGHT,
    emissive: CONFIG.COLOR_CEILING_LIGHT,
    emissiveIntensity: 0.8,
  }),

  // Floor
  floor: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_FLOOR, side: THREE.DoubleSide }),

  // Desk cluster
  desk: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_DESK }),
  deskBase: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_DESK_BASE }),
  deskPartition: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_DESK_PARTITION }),
  monitor: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_MONITOR }),
  monitorScreen: new THREE.MeshStandardMaterial({
    color: CONFIG.COLOR_MONITOR_SCREEN,
    emissive: CONFIG.COLOR_MONITOR_SCREEN,
    emissiveIntensity: 0.6,
  }),
  chair: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_CHAIR }),
  chairMat: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_CHAIR_MAT }),

  // Office furniture
  filingCabinet: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_FILING_CABINET, metalness: 0.3, roughness: 0.6 }),
  waterCooler: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_WATER_COOLER }),
  waterBottle: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_WATER_BOTTLE, transparent: true, opacity: 0.7 }),
  door: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_DOOR }),
  keypad: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_KEYPAD }),
  sideTable: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_SIDE_TABLE }),

  // Plants
  plantPot: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_PLANT_POT }),
  plantLeaves: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_PLANT_LEAVES }),
  plantTrunk: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_PLANT_TRUNK }),

  // Wall decor
  posterBg: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_POSTER_BG }),
  posterText: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_POSTER_TEXT, emissive: CONFIG.COLOR_POSTER_TEXT, emissiveIntensity: 0.2 }),
  clockFace: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_CLOCK_FACE }),
  clockRim: new THREE.MeshStandardMaterial({ color: CONFIG.COLOR_CLOCK_RIM }),

  // Lights
  fluorescent: new THREE.MeshStandardMaterial({
    color: CONFIG.COLOR_FLUORESCENT,
    emissive: CONFIG.COLOR_FLUORESCENT,
    emissiveIntensity: 1.0,
  }),
};
