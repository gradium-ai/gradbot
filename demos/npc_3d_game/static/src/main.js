import * as THREE from 'three';
import { SceneManager } from './engine/SceneManager.js';
import { GameLoop } from './engine/GameLoop.js';
import { GradiumOffice } from './world/GradiumOffice.js';
import { CharacterLoader } from './game/CharacterLoader.js';
import { ThirdPersonController } from './game/ThirdPersonController.js';
import { ThirdPersonCamera } from './game/ThirdPersonCamera.js';
import { InteractionSystem } from './game/InteractionSystem.js';
import { Neil } from './game/Neil.js';
import { IntroSequence } from './game/IntroSequence.js';
import { ClueSystem } from './game/ClueSystem.js';
import { SuspicionSystem } from './game/SuspicionSystem.js';
import { NeilAI } from './game/NeilAI.js';
import { GameUI } from './ui/GameUI.js';
import { VoiceClient } from './network/VoiceClient.js';
import { TTSClient } from './network/TTSClient.js';
import { PlayerAnimator } from './game/PlayerAnimator.js';
import { CONFIG } from './config.js';
import { DebugOverlay } from './debug/DebugOverlay.js';
import { MusicManager } from './audio/MusicManager.js';
import { SFXManager } from './audio/SFXManager.js';

// ── Engine & World ──────────────────────────────────────────
const sceneManager = new SceneManager();
const office = new GradiumOffice(sceneManager.scene);

// ── UI ──────────────────────────────────────────────────────
const gameUI = new GameUI();

// ── Click-to-start overlay ──────────────────────────────────
const overlay = document.createElement('div');
overlay.id = 'start-overlay';
overlay.innerHTML = `
  <div style="
    position: fixed; inset: 0;
    display: flex; align-items: center; justify-content: center;
    background: rgba(0,0,0,0.85);
    color: #e0e0e0;
    font-family: 'Courier New', monospace;
    font-size: 18px;
    cursor: pointer;
    z-index: 300;
    flex-direction: column;
    gap: 12px;
  ">
    <div style="color: #00cc66; font-size: 28px; font-weight: bold; letter-spacing: 4px;">
      GRADIUM INDUSTRIES
    </div>
    <div style="color: #aaa; font-size: 14px;">
      Macrodata Refinement Department
    </div>
    <div style="margin-top: 24px; border: 1px solid #00cc66; padding: 12px 32px;">
      CLICK TO BEGIN YOUR SHIFT
    </div>
    <div style="color: #666; font-size: 12px; margin-top: 16px;">
      WASD to move &bull; Shift to sprint &bull; C to sneak &bull; Mouse to look &bull; E to interact
    </div>
  </div>
`;
document.body.appendChild(overlay);

// ── Interaction system ──────────────────────────────────────
const interactionSystem = new InteractionSystem(
  sceneManager.camera,
  sceneManager.renderer.domElement
);
interactionSystem.enabled = false; // disabled until intro completes

// ── Voice & TTS clients ──────────────────────────────────────
const ttsClient = new TTSClient();
const voiceClient = new VoiceClient({
  onTranscript: (text, isUser) => gameUI.addVoiceTranscript(text, isUser),
  onClueResult: (correct, fragment, attemptsExhausted) => gameUI.showVoiceResult(correct, fragment, attemptsExhausted),
  onCheckinResult: (classification, reason) => {
    console.log('[CheckIn] Classification:', classification, reason);
  },
  onError: (msg) => console.error('Voice:', msg),
});

// ── Music ───────────────────────────────────────────────────
const music = new MusicManager();
const sfx = new SFXManager();

// ── Suspicion system ────────────────────────────────────────
const suspicionSystem = new SuspicionSystem();
suspicionSystem.onLevelChange = (level) => {
  gameUI.updateSuspicion(level);
  music.setSuspicion(level);
  if (level > 0) sfx.playSuspicionSting();
};

// ── Clue system ─────────────────────────────────────────────
const clueSystem = new ClueSystem(gameUI);
let checkWinCondition = () => {}; // assigned after characters load

clueSystem.addClue({
  id: 'note',
  name: 'Strange Note',
  question: 'The numbers are alive. Which department knows the truth?',
  options: [
    'Optics & Design',
    'Macrodata Refinement',
    'Mammalians Nurturable',
    'Disposal & Reclamation',
  ],
  correctIndex: 1,
});

clueSystem.addClue({
  id: 'painting',
  name: "Kier's Portrait",
  question: 'The founder watches over all. What is the name of the procedure that splits the mind in two?',
  options: [
    'The Revolving',
    'The Severance Procedure',
    'The Break Room Protocol',
    'The Lexington Letter',
  ],
  correctIndex: 1,
});

clueSystem.addClue({
  id: 'book',
  name: "Ricken's Book",
  question: 'This forbidden book changed everything. Who is the author of "The You You Are"?',
  options: [
    'Laurent Scout',
    'Ricken Hale',
    'Burt Goodman',
    'Harmony Cobel',
  ],
  correctIndex: 1,
});

// Register interactable objects
const interactableDefs = {
  terminal: {
    name: 'Desk Terminal',
    onInteract: () => {
      sfx.playUIClick();
      gameUI.showSubtitle('System', 'The terminal hums quietly. No active refinement session.', 3000);
    },
  },
  filing_cabinet: {
    name: 'Filing Cabinet',
    onInteract: () => {
      sfx.playFilingCabinet();
      gameUI.showSubtitle('', 'The drawers are locked. Only a department chief can open them.', 3000);
      suspicionSystem.raise(1);
    },
  },
  keypad: {
    name: 'Door Keypad',
    onInteract: () => {
      sfx.playKeypadDeny();
      gameUI.showSubtitle('System', 'ACCESS DENIED. Clearance level insufficient.', 3000);
      suspicionSystem.raise(1);
    },
  },
  water_cooler: {
    name: 'Water Cooler',
    onInteract: () => {
      sfx.playWaterCooler();
      gameUI.showSubtitle('', 'A moment of calm. The water tastes oddly sterile.', 3000);
    },
  },
  painting: {
    name: "Kier's Portrait",
    onInteract: async () => {
      if (clueSystem.isSolved('painting')) {
        gameUI.showSubtitle('', 'The founder stares back, unblinking.', 2000);
        return;
      }
      interactionSystem.enabled = false;
      suspicionSystem.playerInClueInteraction = true;
      const correct = await clueSystem.attemptSolveVoice('painting', voiceClient);
      console.log('[CLUE] painting attemptSolveVoice returned:', correct);
      suspicionSystem.playerInClueInteraction = false;
      interactionSystem.enabled = true;
      if (correct) {
        sfx.playClueChime();
        gameUI.showSubtitle('', "Kier's dream of a world without sorrow... a clean cut.", 3000);
        checkWinCondition();
      }
    },
  },
};

for (const [key, def] of Object.entries(interactableDefs)) {
  const obj = office.interactables.get(key);
  if (obj) interactionSystem.register(obj, def);
}

// Clue: Ricken's Book — proximity trigger near the water cooler
interactionSystem.registerProximity(
  new THREE.Vector3(-6.5, 0, -3.0),
  2.0,
  {
    name: "Ricken's Book",
    onInteract: async () => {
      if (clueSystem.isSolved('book')) {
        gameUI.showSubtitle('', 'You already know who wrote this.', 2000);
        return;
      }
      interactionSystem.enabled = false;
      suspicionSystem.playerInClueInteraction = true;
      const correct = await clueSystem.attemptSolveVoice('book', voiceClient);
      console.log('[CLUE] book attemptSolveVoice returned:', correct);
      suspicionSystem.playerInClueInteraction = false;
      interactionSystem.enabled = true;
      if (correct) {
        sfx.playClueChime();
        gameUI.showSubtitle('', '"Every person is a door." The smuggled book that changed everything.', 3000);
        checkWinCondition();
      }
    },
  }
);

// Clue note — proximity trigger (radius 2 units around the note)
interactionSystem.registerProximity(
  new THREE.Vector3(6.2, 0, 2.8),
  2.0,
  {
    name: 'Strange Note',
    onInteract: async () => {
      if (clueSystem.isSolved('note')) {
        gameUI.showSubtitle('', 'You already deciphered this note.', 2000);
        return;
      }
      interactionSystem.enabled = false;
      suspicionSystem.playerInClueInteraction = true;
      const correct = await clueSystem.attemptSolveVoice('note', voiceClient);
      console.log('[CLUE] note attemptSolveVoice returned:', correct);
      suspicionSystem.playerInClueInteraction = false;
      interactionSystem.enabled = true;
      if (correct) {
        sfx.playClueChime();
        gameUI.showSubtitle('', 'This could be important... you pocket the note.', 3000);
        checkWinCondition();
      }
    },
  }
);

// ── Load characters and start ───────────────────────────────
const loader = new CharacterLoader();
const neil = new Neil();

Promise.all([
  loader.load('assets/glb/severance/mark.glb', {
    scale: CONFIG.CHAR_SCALE,
    castShadow: true,
    receiveShadow: true,
  }),
  neil.load(),
]).then(([markData]) => {
  const { model, mixer, animations } = markData;

  // ── Mark (player) ───────────────────────────────────────
  // Initial position set by IntroSequence (places Mark at desk chair)
  sceneManager.scene.add(model);

  const tpCamera = new ThirdPersonCamera(
    sceneManager.camera,
    sceneManager.renderer.domElement
  );
  tpCamera.setWallMeshes(office.wallMeshes);

  const playerController = new ThirdPersonController(
    model,
    tpCamera,
    sceneManager.renderer.domElement
  );
  playerController.enabled = false; // disabled until intro completes

  // ── Player animation system ────────────────────────────────
  const playerAnimator = new PlayerAnimator(mixer, animations);

  // ── Neil (NPC) ──────────────────────────────────────
  neil.placeAt(2.5, 4.5, Math.PI);
  sceneManager.scene.add(neil.model);

  office.collisionBoxes.push({
    min: new THREE.Vector3(2.5 - 0.35, 0, 4.5 - 0.35),
    max: new THREE.Vector3(2.5 + 0.35, 1.8, 4.5 + 0.35),
  });

  // ── Neil AI ───────────────────────────────────────────
  const neilAI = new NeilAI({
    neil,
    gameUI,
    suspicion: suspicionSystem,
    interactionSystem,
    ttsClient,
    voiceClient,
    clueSystem,
    playerModel: model,
    canvas: sceneManager.renderer.domElement,
    collisionBoxes: office.collisionBoxes,
  });

  // ── Music Dance Experience (win condition) ─────────────────

  // Party light colors (Severance MDE strobe palette)
  const PARTY_COLORS = [
    new THREE.Color(0xff0066), // magenta
    new THREE.Color(0x00ccff), // cyan
    new THREE.Color(0xff6600), // orange
    new THREE.Color(0x9933ff), // purple
    new THREE.Color(0x00ff66), // green
    new THREE.Color(0xffcc00), // gold
  ];
  let partyLightsActive = false;
  const partyPointLights = [];

  function startPartyLights() {
    partyLightsActive = true;

    // Dim office lights over 2 seconds
    const dimDuration = 2000;
    const startTime = performance.now();

    const ambientStart = sceneManager.ambientLight.intensity;
    const hemiStart = sceneManager.hemiLight.intensity;
    const dirStart = sceneManager.dirLight.intensity;
    const ceilingStarts = office.ceilingLights.map(l => l.intensity);

    const dimStep = () => {
      const t = Math.min((performance.now() - startTime) / dimDuration, 1);
      sceneManager.ambientLight.intensity = ambientStart * (1 - t * 0.85);
      sceneManager.hemiLight.intensity = hemiStart * (1 - t * 0.9);
      sceneManager.dirLight.intensity = dirStart * (1 - t * 0.9);
      office.ceilingLights.forEach((l, i) => {
        l.intensity = ceilingStarts[i] * (1 - t * 0.8);
      });
      if (t < 1) requestAnimationFrame(dimStep);
    };
    requestAnimationFrame(dimStep);

    // Add colored party point lights around the room
    const partyPositions = [
      { x: -4, y: 2.5, z: -3 },
      { x:  4, y: 2.5, z: -3 },
      { x: -4, y: 2.5, z:  3 },
      { x:  4, y: 2.5, z:  3 },
      { x:  0, y: 2.8, z:  0 },
      { x:  0, y: 2.5, z:  5 },
    ];
    partyPositions.forEach((pos, i) => {
      const light = new THREE.PointLight(PARTY_COLORS[i], 0, 10);
      light.position.set(pos.x, pos.y, pos.z);
      sceneManager.scene.add(light);
      partyPointLights.push(light);
    });

    // Fade party lights in after office dims
    setTimeout(() => {
      partyPointLights.forEach(l => { l.intensity = 2.0; });
    }, 1500);
  }

  /** Called every frame to cycle party light colors */
  function updatePartyLights(time) {
    if (!partyLightsActive) return;
    partyPointLights.forEach((light, i) => {
      // Each light cycles through colors at different speeds
      const speed = 0.4 + i * 0.15;
      const colorIdx = Math.floor(time * speed + i * 1.5) % PARTY_COLORS.length;
      const nextIdx = (colorIdx + 1) % PARTY_COLORS.length;
      const blend = (time * speed + i * 1.5) % 1;
      light.color.copy(PARTY_COLORS[colorIdx]).lerp(PARTY_COLORS[nextIdx], blend);

      // Pulse intensity for strobe feel
      light.intensity = 1.5 + Math.sin(time * (2 + i * 0.5)) * 0.8;
    });
  }

  function triggerMusicDanceExperience() {
    // Stop all game systems
    clearInterval(window.__timerInterval);
    gameUI.hideTimer();
    neilAI.stop();
    playerController.enabled = false;
    interactionSystem.enabled = false;
    sfx.stopFootsteps();
    sfx.stopOfficeHum();

    // Dismiss any open voice panel and disconnect voice sessions
    // (prevents a mid-flight check-in from covering the victory)
    gameUI.hideVoicePanel();
    voiceClient.disconnect();

    // Phase 1: Dim lights (2s)
    startPartyLights();

    // Phase 2: After lights dim, start dancing + music (2.5s)
    setTimeout(() => {
      playerAnimator.playDance();
      neil.playDance();
      music.playMDE();
    }, 2500);

    // Phase 3: Show victory text after dancing for a while (10s)
    setTimeout(() => {
      gameUI.showVictory();
    }, 10000);
  }

  /** Check if all clues solved and trigger win */
  checkWinCondition = () => {
    console.log('[WIN] checkWinCondition:', clueSystem.solved, '/', clueSystem.total);
    if (clueSystem.solved === clueSystem.total) {
      console.log('[WIN] ALL CLUES SOLVED — triggering MDE!');
      triggerMusicDanceExperience();
    }
  };

  // Wire up game over on caught
  suspicionSystem.onCaught = () => {
    clearInterval(window.__timerInterval);
    gameUI.hideTimer();
    neilAI.stop();
    playerController.enabled = false;
    interactionSystem.enabled = false;
    sfx.stopFootsteps();
    sfx.stopOfficeHum();
    gameUI.hideVoicePanel();
    voiceClient.disconnect();
    music.playGameOver();
    gameUI.showGameOver();
  };

  // Derive clue positions from the already-registered proximity triggers (single source of truth)
  const cluePositions = interactionSystem._proximityTriggers.map(t => t.position);

  // ── Debug overlay ──────────────────────────────────────
  const debugOverlay = new DebugOverlay(
    sceneManager.scene,
    sceneManager.camera,
    sceneManager.renderer.domElement,
    {
      collisionBoxes: office.collisionBoxes,
      interactionSystem,
      playerModel: model,
      playerRadius: CONFIG.PLAYER_RADIUS,
    }
  );

  // Sprint suspicion cooldown
  let sprintSuspicionTimer = 0;

  // ── Game loop ───────────────────────────────────────────
  const gameLoop = new GameLoop((dt) => {
    mixer.update(dt);
    model.position.y = 0; // Clamp after mixer — prevent animation root motion from sinking Mark
    neil.update(dt);

    // Track if player is near a clue location (risk signal) — compute before controller update
    let nearClue = false;
    const nearR = CONFIG.NEAR_CLUE_RADIUS;
    for (const pos of cluePositions) {
      const dx = model.position.x - pos.x;
      const dz = model.position.z - pos.z;
      if (dx * dx + dz * dz < nearR * nearR) {
        nearClue = true;
        break;
      }
    }

    // Feed context to controller before update (affects sneak/sprint decision)
    playerController.nearClue = nearClue;
    playerController.update(dt, office.collisionBoxes);

    // Update player animations
    playerAnimator.update(dt, {
      movementState: playerController.movementState,
      speedFactor: playerController.speedFactor,
      nearClue,
      idleTime: playerController.idleTime,
    });

    // Footstep SFX
    sfx.updateFootsteps(playerController.movementState);

    // Sync suspicion flags
    suspicionSystem.playerNearClue = nearClue;
    suspicionSystem.playerSneaking = playerController.isSneaking;

    // Sprint near Neil raises suspicion (with cooldown)
    sprintSuspicionTimer = Math.max(0, sprintSuspicionTimer - dt);
    if (playerController.isSprinting && sprintSuspicionTimer <= 0) {
      const milPos = neil.model.position;
      const dx = model.position.x - milPos.x;
      const dz = model.position.z - milPos.z;
      if (dx * dx + dz * dz < CONFIG.SPRINT_SUSPICION_RADIUS * CONFIG.SPRINT_SUSPICION_RADIUS) {
        suspicionSystem.raise(1);
        sprintSuspicionTimer = CONFIG.SPRINT_SUSPICION_COOLDOWN;
      }
    }

    tpCamera.update(model.position, dt);
    interactionSystem.update(model.position);

    // Update Neil AI check-in scheduler
    neilAI.update(dt);

    // Party lights color cycling
    updatePartyLights(performance.now() / 1000);

    debugOverlay.update();
    sceneManager.render();
  });

  gameLoop.start();

  // ── Start game on click ─────────────────────────────────
  let gameStarted = false;
  overlay.addEventListener('click', async () => {
    if (gameStarted) return; // prevent double-start
    gameStarted = true;
    overlay.style.display = 'none';

    // Request pointer lock for camera look during intro
    try {
      await sceneManager.renderer.domElement.requestPointerLock();
    } catch (e) {
      console.warn('Pointer lock not yet acquired, will retry after intro');
    }

    // Run intro sequence (player controls stay disabled)
    const intro = new IntroSequence(gameUI, neil, ttsClient, playerAnimator, model);
    await intro.play();

    // Hand control to the player
    playerController.enabled = true;
    interactionSystem.enabled = true;
    music.start();
    sfx.startOfficeHum();

    // Ensure pointer lock after intro
    if (!document.pointerLockElement) {
      try { await sceneManager.renderer.domElement.requestPointerLock(); } catch (_) {}
    }

    gameUI.setObjective('Find hidden clues without Neil noticing.');
    gameUI.updateClueCounter(clueSystem.solved, clueSystem.total);
    gameUI.showControlsHint();

    // Start 3-minute countdown timer
    let timeLeft = 180;
    gameUI.updateTimer(timeLeft);
    const timerInterval = setInterval(() => {
      timeLeft--;
      gameUI.updateTimer(timeLeft);
      if (timeLeft <= 0) {
        clearInterval(timerInterval);
        gameUI.hideTimer();
        // Time's up — trigger game over
        neilAI.stop();
        playerController.enabled = false;
        interactionSystem.enabled = false;
        sfx.stopFootsteps();
        sfx.stopOfficeHum();
        music.playGameOver();
        gameUI.showGameOver('TIME\'S UP', 'You ran out of time. The Board is disappointed.');
      }
    }, 1000);

    // Store interval so victory can clear it
    window.__timerInterval = timerInterval;

    // Start Neil's check-in loop
    neilAI.start();

    // Expose for automated testing
    window.__test = {
      playerModel: model, neil, neilAI, suspicionSystem,
      clueSystem, gameUI, checkWinCondition, triggerMusicDanceExperience,
    };

  });

  // Re-show overlay when pointer lock is lost (only before game starts)
  document.addEventListener('pointerlockchange', () => {
    if (!document.pointerLockElement && !gameStarted) {
      overlay.style.display = 'block';
    }
  });

  // Click canvas to re-acquire pointer lock after game has started
  sceneManager.renderer.domElement.addEventListener('click', () => {
    if (gameStarted && !document.pointerLockElement) {
      sceneManager.renderer.domElement.requestPointerLock();
    }
  });

}).catch((err) => {
  console.error('Failed to load characters:', err);
});
