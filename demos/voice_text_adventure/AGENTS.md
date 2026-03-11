# Voice Text Adventure - Agent Guide

## Project Overview

**Voice Text Adventure** is a demo that integrates the Jericho text adventure engine with Gradbot's voice AI capabilities. The system allows users to play classic text adventure games (Zork, Colossal Cave Adventure, etc.) using voice commands.

## Core Concept

1. **Jericho Integration**: Uses the `jericho` Python package to run text adventure games
2. **Voice Agent**: Gradbot voice agent reads game descriptions aloud and accepts voice commands
3. **Command Validation**: Agent validates commands against game's valid command list
4. **Error Correction**: If ASR misinterprets a command, agent asks for confirmation

## Architecture

```
┌─────────────────┐    ┌─────────────────┐    ┌─────────────────┐
│   Web Frontend  │◄──►│  FastAPI Server │◄──►│  Jericho Game   │
│  (Browser UI)   │    │   (main.py)     │    │    Engine       │
└─────────────────┘    └─────────────────┘    └─────────────────┘
         │                       │                       │
         └───────────────────────┼───────────────────────┘
                                 │
                          ┌─────────────────┐
                          │   Gradbot      │
                          │ Voice Agent     │
                          │ (gradbot)    │
                          └─────────────────┘
```

## Game Flow

1. **Initialization**: User selects adventure game from available list
2. **Game Start**: Voice agent reads initial game description + valid commands
3. **Voice Interaction**:
   - Agent reads game state descriptions aloud
   - Agent provides list of valid commands (for reference)
   - User speaks commands
   - Agent validates against valid command list
4. **Command Processing**:
   - If command matches valid command: pass to Jericho via function call
   - If command sounds similar but not exact: ask for confirmation/correction
   - If not a command: free conversation (agent can chat about game, hints, etc.)

## Required Files

### ✅ Completed:
- `pyproject.toml` - Python dependencies (FastAPI, uvicorn, websockets, gradbot, jericho)

### ❌ Missing:
- `main.py` - FastAPI server with Jericho integration
- `README.md` - Documentation
- `static/index.html` - Frontend UI
- `TODO.md` - Implementation tracking

## Implementation Details

### Main.py Requirements:
1. **Game Management**:
   - List available Jericho games
   - Initialize game instance
   - Track game state

2. **Voice Agent Integration**:
   - Create Gradbot session with appropriate tools
   - Tool: `execute_command(command: str)` - passes command to Jericho
   - Tool: `list_valid_commands()` - returns current valid commands
   - Tool: `get_game_state()` - returns current game description

3. **WebSocket Protocol**:
   - Similar to other demos but with game-specific messages
   - Messages: `game_list`, `start_game`, `game_state`, `command_result`

4. **Command Validation Logic**:
   - Compare transcribed command with valid command list
   - Fuzzy matching for ASR errors
   - Confirmation dialog for ambiguous commands

### Frontend Requirements:
- Game selection dropdown
- Display game state (description + valid commands)
- Voice controls (start/stop recording)
- Transcript display
- Command history

## Crash Recovery

If Crush CLI crashes, resume by:
1. Checking `TODO.md` for remaining tasks
2. Reviewing this `AGENTS.md` for context
3. Continuing implementation from last completed item

## Testing Plan

1. **Basic Integration**: Load a Jericho game, verify initialization
2. **Voice Commands**: Test command execution via voice
3. **Error Handling**: Test ASR correction flow
4. **Full Gameplay**: Play through a simple adventure scenario

## Notes

- Jericho provides `get_valid_actions()` for command validation
- Games are typically Z-machine format (`.z5`, `.z8` files)
- Consider adding game save/load functionality
- May need to handle game-specific vocabulary/commands

## References

- Jericho Python package documentation
- Gradbot demos (simple_chat, fantasy_shop) for WebSocket patterns
- Existing `static/js/` files already present (audio processing)