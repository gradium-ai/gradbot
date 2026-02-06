# Voice Text Adventure - Implementation TODO

## ✅ Completed
- [x] **Project Structure**: Created `voice_text_adventure/` directory
- [x] **Dependencies**: Created `pyproject.toml` with required packages (FastAPI, uvicorn, websockets, pygradbot, jericho)
- [x] **Documentation**: Created `AGENTS.md` with project context for crash recovery
- [x] **Core Server**: Created `main.py` with Jericho integration and Gradbot voice agent
- [x] **Frontend**: Created `static/index.html` for game selection and voice interface
- [x] **JS Setup**: Symlinked `synced-audio-player.js` to static/js

### Main.py Implementation (Complete)
- [x] Import required packages (fastapi, jericho, pygradbot, etc.)
- [x] Create FastAPI app with lifespan management
- [x] Implement game management:
  - [x] List available Jericho games (`/api/games`)
  - [x] Game initialization/state tracking (GameState dataclass)
- [x] Create Gradbot tool definitions:
  - [x] `execute_command(command: str)` - passes command to Jericho
  - [x] `get_valid_commands()` - returns current valid commands
  - [x] `get_game_state()` - returns game description
- [x] Implement WebSocket handler (`/ws/game`):
  - [x] Game selection protocol
  - [x] Game state updates
  - [x] Command execution/results

### Frontend Implementation (Complete)
- [x] Create `static/index.html` with:
  - [x] Game selection dropdown
  - [x] Game state display area
  - [x] Valid commands list
  - [x] Voice controls (start/stop recording)
  - [x] Transcript display
- [x] Add JavaScript for:
  - [x] WebSocket connection management
  - [x] Audio processing (using existing `static/js/` files)
  - [x] UI updates for game state

## 🚧 In Progress
- [ ] **Testing**: Test the full integration
- [ ] **Documentation**: Create `README.md` with setup and usage instructions

## 📋 Next Steps (Optional Enhancements)

### Testing & Polish
- [ ] Test basic Jericho game loading
- [ ] Test voice command execution
- [ ] Test ASR error correction flow
- [ ] Add fuzzy command matching for ASR errors
- [ ] Add confirmation flow for ambiguous commands
- [ ] Add game save/load functionality

## 🔄 Progress Updates

**2025-01-29**: Initial setup complete. Created project structure and documentation. Ready to implement main server logic.

**2025-01-29**: Core implementation complete! Created main.py with full Jericho integration, Gradbot voice agent with tools, and index.html frontend with retro terminal styling. Ready for testing.

## 📝 Notes

- Existing `static/js/` files already contain audio processing code (reuse from other demos)
- Follow patterns from `simple_chat` and `fantasy_shop` demos for WebSocket protocol
- Jericho games are typically in Z-machine format (`.z5`, `.z8` files)
- Consider adding game save/load functionality as enhancement

## 🎯 Success Criteria

Demo is complete when:
1. User can select a Jericho adventure from list
2. Voice agent reads initial game description aloud
3. User can issue voice commands that execute in the game
4. Agent validates commands against valid command list
5. ASR errors trigger confirmation/correction flow
6. Full voice-based gameplay is possible