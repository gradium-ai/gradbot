export const CONFIG = {
  // Room dimensions (single rectangular office)
  ROOM_WIDTH: 16,
  ROOM_DEPTH: 12,
  WALL_HEIGHT: 3.2,
  WALL_THICKNESS: 0.3,

  // Colors — Gradium palette (Severance-inspired)
  COLOR_WALL: 0xf0ede8,          // Off-white paneled walls
  COLOR_WALL_PANEL_LINE: 0xe0ddd6, // Panel seam lines
  COLOR_WALL_TRIM: 0x2a2a2e,     // Dark trim strip at top/bottom
  COLOR_FLOOR: 0x3a7a3a,         // Green carpet
  COLOR_CEILING: 0xe8e4dc,       // Light ceiling
  COLOR_CEILING_LIGHT: 0xf8f8f0, // Recessed light panel
  COLOR_DESK: 0xe8e0d4,          // Cream/beige desk surface
  COLOR_DESK_BASE: 0x1a1a1a,     // Dark desk base/legs
  COLOR_DESK_PARTITION: 0x3a6a3a, // Green desk partitions
  COLOR_MONITOR: 0xd8d0c4,       // Beige CRT monitor
  COLOR_MONITOR_SCREEN: 0x1a3a2a, // Dark green screen
  COLOR_CHAIR: 0x2a2a2a,         // Dark office chair
  COLOR_CHAIR_MAT: 0x333333,     // Dark circular chair mat
  COLOR_FILING_CABINET: 0x909090, // Grey filing cabinet
  COLOR_WATER_COOLER: 0xc8d8e8,  // Light blue water cooler
  COLOR_WATER_BOTTLE: 0x4488cc,  // Blue water bottle
  COLOR_DOOR: 0x3a6a3a,          // Green double door
  COLOR_KEYPAD: 0x2a2a2a,        // Dark keypad
  COLOR_SIDE_TABLE: 0x8b5e3c,    // Wooden side table
  COLOR_PLANT_POT: 0xe8dcc8,     // Beige pot
  COLOR_PLANT_LEAVES: 0x2d5a2d,  // Dark green leaves
  COLOR_PLANT_TRUNK: 0x6b4226,   // Brown trunk
  COLOR_POSTER_BG: 0x1a1a1a,     // Dark poster background
  COLOR_POSTER_TEXT: 0x3a7a3a,   // Green poster text
  COLOR_CLOCK_FACE: 0xffffff,    // White clock face
  COLOR_CLOCK_RIM: 0x2a2a2a,     // Dark clock rim
  COLOR_FLUORESCENT: 0xf5f5e8,
  COLOR_GRADIUM_GREEN: 0x00cc66,

  // Player / first-person
  PLAYER_SPEED: 3.0,             // Movement speed (units/sec)
  PLAYER_RADIUS: 0.35,
  PLAYER_EYE_HEIGHT: 1.6,
  MOUSE_SENSITIVITY: 0.002,
  WALK_ANIM_SPEED: 1.41,        // Walk clip's authored speed (units/sec from Mixamo root motion)

  // Sneak mode (Shift near clues)
  SNEAK_SPEED: 1.2,
  SNEAK_ANIM_SPEED: 1.0,
  SNEAK_SUSPICION_MULT: 0.3,     // Suspicion gain multiplier when sneaking

  // Sprint mode (Shift away from clues)
  SPRINT_SPEED: 5.5,
  SPRINT_ANIM_SPEED: 2.5,
  SPRINT_SUSPICION_RADIUS: 5.0,  // Sprinting within this range of Neil raises suspicion
  SPRINT_SUSPICION_COOLDOWN: 5,  // Seconds between sprint-suspicion ticks

  // Look Around
  LOOK_AROUND_IDLE_THRESHOLD: 4.0,  // Seconds idle near a clue before auto look-around

  // Camera
  CAM_FOV: 60,
  CAM_NEAR: 0.1,
  CAM_FAR: 100,

  // Third-person camera
  CAMERA_3P: {
    DISTANCE: 2.8,            // Distance behind the character
    HEIGHT: 1.45,             // Shoulder-level pivot height
    SHOULDER_OFFSET_X: 0.4,   // Slight right offset for over-the-shoulder
    LOOK_AT_OFFSET_Y: 0.1,    // Look slightly above pivot (chest area)
    SMOOTHING: 0.15,          // Camera follow smoothing (0–1, higher = snappier)
  },

  // Character
  CHAR_SCALE: 0.95,            // Uniform scale for Mark (Mixamo)
  NEIL_SCALE: 0.995,       // Slightly larger to match Mark's height (1.821 * 0.995 ≈ 1.905 * 0.95)
  CHAR_TURN_SPEED: 6,          // Rotation smoothing speed (rad/s factor, lower = smoother turns)
  PLAYER_ACCEL: 5,             // Movement acceleration (units/s², for smooth start/stop)
  PLAYER_DECEL: 8,             // Movement deceleration (units/s², for smooth stop)

  // Interaction
  INTERACT_RANGE: 4.0,

  // Suspicion
  SUSPICION_MAX: 3,

  // Neil AI
  NEIL_AI: {
    FIRST_CHECKIN_DELAY: 20,       // seconds after game start before first check-in
    CHECKIN_INTERVAL_MIN: 35,      // minimum seconds between check-ins
    CHECKIN_INTERVAL_MAX: 45,      // maximum seconds between check-ins
    APPROACH_DURATION: 3,          // seconds for approach animation
    LEAVE_DURATION: 2.5,           // seconds for leaving animation
    WARNING_LEAD_TIME: 3,          // seconds of "footsteps" warning before arrival
    APPROACH_DISTANCE: 2.5,        // how close Neil gets to player
    WALK_SPEED: 3.0,               // Neil walk speed in units/sec
    VOICE_TIMEOUT: 12000,          // ms to wait for voice classification
  },

  // Suspicion near-clue radius (matches proximity trigger radius)
  NEAR_CLUE_RADIUS: 3.0,

  // Neil home position
  NEIL_HOME: { x: 2.5, z: 4.5 },

  // Game loop
  MAX_DELTA: 0.1,
};
