---
name: gradbot-voice-agent
description: Build complete, working voice agent applications using the gradbot framework. Use when user asks to "build a voice assistant", "create a voice agent", "make a voice app", "build a haggling game", or any voice-interactive application with speech. Generates a FastAPI backend with STT/LLM/TTS orchestration, tool calling, system prompts, and a polished frontend UI. Works for any domain - customer service, games, booking, tutoring, roleplay, etc. Also use when user is working with an existing gradbot demo and wants to modify, extend, or understand it.
---

# Gradbot Voice Agent Builder

Build working voice agent apps using the gradbot framework. Output is a complete, runnable app with backend, frontend, prompts, and configuration.

## Important

- ALWAYS read `references/backend-template.md` and `references/frontend-guidelines.md` before generating code
- The generated app MUST follow the exact patterns from the reference files — these are battle-tested
- Use the `frontend-design` skill to generate the `static/index.html` — do NOT write a generic HTML file

## Two Complexity Levels

### Simple chat (no tools)

For apps that are purely conversational with no actions/side-effects:
- No tools, no tool handler, no state dataclass needed
- Do NOT pass `on_tool_call` to `gradbot.websocket.handle_session()` (omit it entirely)
- Prompt can come from a file OR from the frontend (editable textarea)
- No `prompts/` directory needed if prompt comes from frontend
- Pass `with_voices=True` to `gradbot.routes.setup()` to enable voice selection UI
- See the "Minimal (no tools)" template in `references/backend-template.md`

### Tool-using agent (with tools)

For apps with actions (ordering, searching, game mechanics, etc.):
- Define tools via `gradbot.ToolDef` and implement tool handlers
- Track state in a dataclass, update it in tool handlers
- System prompts loaded from `prompts/` files
- Include `on_tool_call` callback in `gradbot.websocket.handle_session()`
- See the "With tools" template in `references/backend-template.md`

## Workflow

### Step 1: Understand the Use Case

Before writing code, identify:
1. **Domain**: What is the voice agent about?
2. **Persona**: Who is the AI character? What personality?
3. **Tools needed**: What actions can it perform? (If none, use simple chat pattern)
4. **State**: What data to track across the conversation? (If none, use simple chat)
5. **UI needs**: What should the frontend show besides transcript?

### Step 2: Create the App Directory

**Simple chat** (no tools, prompt from frontend):
```
<app-dir>/
  main.py
  static/
    index.html
  pyproject.toml
```

**Tool-using agent** (tools, file-based prompts):
```
<app-dir>/
  main.py
  game.py          # domain module (state, tools, tool handler)
  prompts/
    main.txt       # (or base.txt + phase1.txt, phase2.txt, etc.)
  static/
    index.html
  pyproject.toml
```

If the app has static data (menus, inventories, etc.), include it as JSON files.

### Step 3: Write the Backend (main.py)

Consult `references/backend-template.md` for exact code patterns. Choose the right template:
- **Minimal (no tools)**: ~30 lines in a single `main.py`, voice selection, frontend-editable prompt
- **With tools**: thin `main.py` (~20 lines) + domain module (`game.py`, ~100+ lines) with state, tools, prompts, and tool handler

Key setup rules:
- Use `gradbot.config.load(Path(__file__).parent)` or `gradbot.config.from_env()` for config loading — it loads `config.yaml` from the app directory and any shared parent config automatically. Returns a `Config` object with `.client_kwargs` and `.session_kwargs` properties.
- ALWAYS call `gradbot.routes.setup(app, config=cfg, static_dir=...)` to serve frontend and bundled JS
- Pass `with_voices=True` to `gradbot.routes.setup()` if the frontend needs voice selection (registers `/api/voices`)
- `rewrite_rules` enables language-specific text rewriting before TTS. Get it from `voice.language.rewrite_rules` (returns `"en"`, `"fr"`, etc.). Do NOT use `.value` — `Lang` is not a Python enum.
- ALWAYS set `silence_timeout_s` to `0.0` — the default 5s causes the agent to re-prompt itself with its last message when the user is silent
- Pass `config=cfg` to `handle_session()` to auto-set `run_kwargs`, `output_format`, and `debug` from the config. Or pass them individually for custom setups.

Critical rules for tool definitions (when using tools):
- `parameters_json` must be a JSON **string** — use `json.dumps()`
- NEVER use `"type": "array"` in parameters (some LLMs like Gemma fail). Use `"type": "string"` with `"description": "Comma-separated list"` instead
- Tool descriptions should say WHEN to call the tool, not just what it does

Critical rules for tool handlers (when using tools):
- `on_tool_call` receives 3 args: `(handle, input_handle, websocket)` where `handle` is a `gradbot.ToolHandle`
- `handle.name` gives the tool name, `handle.args` gives parsed args (already deserialized dict)
- Send results via `handle.send_json({...})` (auto-serializes) or `handle.send(json.dumps({...}))` for raw JSON
- Send errors via `handle.send_error("message")`
- Send UI updates via `websocket.send_json({"type": "custom_event", ...})`
- Use `input_handle.send_config(new_config)` to swap prompts/tools mid-session

Critical rules for system prompts:
- Keep responses SHORT: "1-2 sentences max" for voice (long text = slow TTS)
- If using tools: include "Call tools silently FIRST, then speak"
- Be explicit about what the agent should NOT do

### Step 4: Write System Prompts (tool-using agents only)

Skip this step for simple chat apps where the prompt comes from the frontend.

For tool-using agents, create `prompts/base.txt` and `prompts/main.txt`:
- base.txt: personality, speaking style, boundaries, response length
- main.txt: conversation flow, when to call tools, error handling

For multi-phase apps, create phase1.txt, phase2.txt, etc. and swap via `input_handle.send_config()`.

### Step 5: Build the Frontend

IMPORTANT: Invoke the `frontend-design` skill to create `static/index.html`.

When invoking frontend-design, provide these requirements:
- It's a voice agent UI — primary interaction is speech, not typing
- Must include a mic/call button to start/stop the session
- Must show a live transcript (user bubbles + agent bubbles)
- CRITICAL: Must load audio via three script tags (NOT ES module imports): `opus-encoder.js`, `audio-processor.js`, `synced-audio-player.js` from `/static/js/` — then use `SyncedAudioPlayer` as a global (it is NOT an ES module)
- Must follow the WebSocket protocol in `references/frontend-guidelines.md`
- Must include echo cancellation checkbox (checked by default) — without it the agent hears its own TTS
- For simple chat: include voice selector grid, editable prompt textarea, speed slider
- For tool-using agents: include domain-specific content panels (menu, inventory, results, etc.)

### Step 6: Create pyproject.toml

```toml
[project]
name = "<app-name>"
version = "0.1.0"
description = "<description>"
requires-python = ">=3.12"
dependencies = ["gradbot"]
```

`gradbot` includes fastapi, uvicorn with websocket support, pydantic-settings, and pyyaml. Add extra dependencies only if needed.

### Step 7: Verify

After generating all files:
- [ ] `main.py` imports `gradbot` (uses `gradbot.websocket`, `gradbot.routes`, `gradbot.config`)
- [ ] Uses `gradbot.config.load(Path(__file__).parent)` or `gradbot.config.from_env()` for configuration
- [ ] `gradbot.routes.setup()` called with `static_dir` and `with_voices=True` (if voice selection needed)
- [ ] `rewrite_rules` uses `voice.language.rewrite_rules` (not `.value`)
- [ ] `silence_timeout_s` set to `0.0`
- [ ] WebSocket endpoint calls `gradbot.websocket.handle_session()` with correct callbacks
- [ ] If no tools: `on_tool_call` is NOT passed to `handle_session()`
- [ ] If tools: `parameters_json` is a JSON string via `json.dumps()`, tool results via `handle.send_json()` or `handle.send(json.dumps())`
- [ ] If tools: `on_tool_call` takes 3 args `(handle, input_handle, websocket)`, uses `handle.name` and `handle.args`
- [ ] Frontend loads JS via three `<script>` tags, NOT ES module imports
- [ ] Frontend `onText` destructures a single object: `({ text, turnIdx, isUser }) =>`
- [ ] Frontend has echo cancellation checkbox wired to `SyncedAudioPlayer`
- [ ] System prompt enforces short responses for voice

## Common Patterns

### Simple Voice Chat (no tools)
- No state, no tools, no prompts/ directory
- Voice selection + editable prompt from frontend
- Frontend: transcript + voice grid + speed slider
- ~40 lines of backend code

### Voice Game (haggling, trivia, roleplay)
- State tracks game progress, scores, inventory
- Tools: game actions (buy, sell, attack, answer)
- Prompt: character personality + game rules + win/lose conditions
- Frontend: game state display (inventory, score, health bar)

### Customer Service Agent (ordering, booking, support)
- State tracks order/booking details
- Tools: CRUD operations (add, remove, modify, confirm)
- Prompt: service persona + menu/catalog knowledge + ordering rules
- Frontend: order summary, menu display, confirmation

### Tutoring / Language Learning
- State tracks lesson progress, mistakes, topics covered
- Tools: check answer, advance lesson, provide hint
- Prompt: teacher persona + curriculum + encouragement style
- Frontend: lesson content, progress tracker, exercise display

### Search-and-Act Agent (hotel booking, product search)
- State tracks search results and selections
- Tools: search, get details, book/purchase
- Prompt: phases that swap as user progresses through workflow
- Frontend: search results cards, detail view, confirmation

## Troubleshooting

### Agent gives long responses
Add to system prompt: "CRITICAL: Keep ALL responses to 1-2 SHORT sentences. This is a voice conversation, not a text chat."

### Agent repeats itself / re-prompts when user is silent
Set `silence_timeout_s = 0.0` in the session config. The default 5s timeout causes the agent to re-send its last message as context and generate a new response.

### Tool calls fail with "Invalid JSON"
`tool_handle.send()` requires a valid JSON string. Always use `json.dumps({...})`, never pass a raw string.

### Tool calls fail silently
Ensure `on_tool_call` is passed to `handle_session()`. Check that `handle.send()` is called with a JSON string, or use `handle.send_json()` with a dict.

### Audio doesn't play
Verify frontend loads JS via three script tags (`opus-encoder.js`, `audio-processor.js`, `synced-audio-player.js`), NOT via ES module import. `SyncedAudioPlayer` is a global.

### Transcript shows [object Object]
The `onText` callback receives a single object, not separate args. Must destructure: `({ text, turnIdx, isUser }) =>`.

### Agent hears its own voice (feedback loop)
Add an echo cancellation checkbox and wire it to `SyncedAudioPlayer({ echoCancellation: checkbox.checked })`.

### Voice selection not working
Ensure `gradbot.routes.setup(app, ..., with_voices=True)` is passed. Without `with_voices=True`, the `/api/voices` endpoint is not registered.

### WebSocket returns 404 / "Unsupported upgrade request"
Uvicorn needs the `websockets` library. Ensure you're using `gradbot[demos]` (which includes `uvicorn[standard]`). If still failing, add `websockets` explicitly to your dependencies.

### Wrong language TTS pronunciation
Set `rewrite_rules` to `voice.language.rewrite_rules`. This enables language-specific text rewriting before synthesis.
