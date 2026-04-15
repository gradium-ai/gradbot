# Vibe Coding a Voice Agent with Claude

You can build a fully working voice AI agent in under an hour using Claude and `gradbot`. No boilerplate, no complex pipelines — just describe what you want your agent to do, and Claude writes the code.

This guide is everything you need. It covers the API, the patterns, and — most importantly — the prompt engineering lessons we've learned from building a dozen voice demos.

## What you're building

A real-time voice agent: the user talks, your agent listens (STT), thinks (LLM), speaks back (TTS), and can call tools mid-conversation. All coordinated by `gradbot`, which handles the audio pipeline so you only write Python.

## Setup

### Option A: pip install

```bash
pip install gradbot
# or
uv pip install gradbot
```

### Option B: clone the repo

```bash
git clone git@github.com:gradium-ai/gradbot.git
cd gradbot/demos/simple_chat
uv sync
```

The repo includes 10 working demos you can study, modify, or use as starting points.

### Get your API keys

gradbot uses the Gradium API for STT and TTS orchestration. Register at [gradium.ai](https://gradium.ai) to get an API key with free credits to get started.

We do not provide the LLM — bring your own. Any OpenAI-compatible API will work. We recommend a model that handles tool calls properly but is small enough to be fast and non-thinking. We've had success with Mistral and Qwen.

```bash
export GRADIUM_API_KEY="your-key"
export GRADIUM_BASE_URL="https://api.gradium.ai"
export LLM_API_KEY="your-key"
export LLM_BASE_URL="https://api.openai.com/v1"
export LLM_MODEL="your-model"
```

## The minimal voice agent

Here's the smallest working agent — about 60 lines of actual logic:

```python
import asyncio
import json
from fastapi import FastAPI, WebSocket
import gradbot

gradbot.init_logging()
app = FastAPI()

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    await websocket.accept()

    # Pick a voice
    voice = gradbot.flagship_voice("Emma")

    # Configure the session
    config = gradbot.SessionConfig(
        voice_id=voice.voice_id,
        instructions="You are a friendly assistant. Keep responses to 2-3 sentences.",
        language=voice.language,
        rewrite_rules=voice.language.rewrite_rules,
        flush_duration_s=0.5,
    )

    # Start the voice pipeline
    input_handle, output_handle = await gradbot.run(
        session_config=config,
        input_format=gradbot.AudioFormat.OggOpus,
        output_format=gradbot.AudioFormat.OggOpus,
    )

    stop = asyncio.Event()

    async def send_output():
        while not stop.is_set():
            msg = await output_handle.receive()
            if msg is None:
                break
            if msg.msg_type == "audio":
                await websocket.send_bytes(msg.data)
            elif msg.msg_type == "tts_text":
                await websocket.send_json({"type": "transcript", "text": msg.text, "is_user": False})
            elif msg.msg_type == "stt_text":
                await websocket.send_json({"type": "transcript", "text": msg.text, "is_user": True})

    async def receive_input():
        while not stop.is_set():
            msg = await websocket.receive()
            if "bytes" in msg:
                await input_handle.send_audio(msg["bytes"])
            elif "text" in msg:
                data = json.loads(msg["text"])
                if data.get("type") == "stop":
                    stop.set()
                    await input_handle.close()
                    break

    await asyncio.gather(send_output(), receive_input(), return_exceptions=True)
```

Run it:
```bash
uvicorn main:app --reload
```

That's a working voice agent. The browser sends audio over WebSocket, gradbot coordinates STT → LLM → TTS, and you get audio back.

## The API surface

### Voices

```python
# List all available voices
voices = gradbot.flagship_voices()

# Pick one by name
voice = gradbot.flagship_voice("Emma")
# voice.name          → "Emma"
# voice.voice_id      → "YTpq7expH9539ERJ"
# voice.language      → Lang.En
# voice.country       → Country.Us
# voice.gender        → Gender.Feminine
# voice.description   → "Warm, professional voice..."
```

### SessionConfig

```python
config = gradbot.SessionConfig(
    voice_id=voice.voice_id,           # Required: which voice to use
    instructions="...",                 # Required: system prompt
    language=voice.language,            # Required: Lang.En, Fr, Es, De, Pt
    tools=[...],                        # Optional: list of ToolDef
    rewrite_rules=voice.language.rewrite_rules,  # TTS text normalization
    flush_duration_s=0.5,              # STT silence threshold (lower = faster)
    padding_bonus=0.0,                 # -4 to 4, positive = more patience
    silence_timeout_s=5.0,             # Silence before re-prompting
    assistant_speaks_first=True,       # Agent greets on connect
)
```

### Tools

```python
tool = gradbot.ToolDef(
    name="get_weather",
    description="Get current weather for a city",
    parameters_json=json.dumps({
        "type": "object",
        "properties": {
            "city": {"type": "string", "description": "City name"},
        },
        "required": ["city"],
    }),
)
```

Note: `parameters_json` is a JSON **string**, not a dict.

### Handling tool calls

Tool calls arrive in the output loop:

```python
async def send_output():
    while not stop.is_set():
        msg = await output_handle.receive()
        if msg is None:
            break

        if msg.msg_type == "tool_call":
            asyncio.create_task(
                handle_tool(msg.tool_call, msg.tool_call_handle)
            )
        elif msg.msg_type == "audio":
            await websocket.send_bytes(msg.data)
        # ... other message types

async def handle_tool(tool_call, tool_handle):
    args = json.loads(tool_call.args_json)

    result = await fetch_weather(args["city"])

    # Send result back to the LLM
    await tool_handle.send(json.dumps(result))

    # Or send an error
    # await tool_handle.send_error("City not found")
```

Always handle tool calls in a separate `asyncio.create_task()` — never block the output loop.

### Changing config mid-conversation

You can swap voices, prompts, or tools during a live session:

```python
new_voice = gradbot.flagship_voice("Leo")
new_config = gradbot.SessionConfig(
    voice_id=new_voice.voice_id,
    instructions=new_prompt,
    language=new_voice.language,
    tools=updated_tools,
    rewrite_rules=new_voice.language.rewrite_rules,
)
await input_handle.send_config(new_config)
```

This is how voice switching and multi-phase conversations work.

## Telling Claude to build your agent

Here's the key insight: **Claude can build your entire agent if you give it this blog post as context.** Paste this guide into your conversation or reference it in a CLAUDE.md file, then describe what you want:

> "Build me a voice agent that helps users practice job interviews. It should ask common interview questions, give feedback on answers, and switch between friendly and tough interviewer personas."

Claude will scaffold the FastAPI app, define the tools, write the system prompt, and wire everything together.

For more complex agents, start from one of the existing demos:

> "Look at demos/hotel/main.py. Build me something similar but for restaurant reservations. Use the same multi-phase prompt pattern."

## Prompt engineering for voice — what we've learned

This is the section that matters most. Writing prompts for voice agents is different from writing prompts for chatbots. Here's everything we've learned from building 11 demos.

### 1. Structure your prompt in layers

Every good voice prompt follows this structure:

```
1. IDENTITY (who you are)
2. CAPABILITIES (what tools you have)
3. SPEAKING STYLE (how you talk)
4. DOMAIN RULES (what you know and don't know)
5. TOOL USAGE RULES (when and how to call tools)
```

Here's a real example from our hotel booking agent:

```
You are Sophie, a warm and knowledgeable hotel reservation agent
at Wanderlust Travel.

YOUR PERSONALITY:
- Warm, professional, and genuinely enthusiastic about travel
- You make callers feel like they're talking to a well-traveled friend

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *smiles* or *typing*
- Be conversational and natural, like a real phone call

NEVER FABRICATE DATA:
- NEVER make up hotel names, room types, prices, or availability
- ONLY present information from tool call results
- If a tool result hasn't arrived, you DO NOT have the data

WHILE WAITING FOR TOOL RESULTS:
- Do NOT ask questions — results arrive in 10-20s, caller won't have time to answer
- Instead, SHARE information: fun facts, tips, destination highlights
```

### 2. Be prescriptive, not permissive

Bad: "You can use the search tool when the user asks about hotels."

Good: "The INSTANT the caller mentions a city or destination, you MUST call search_hotels. Do NOT ask more questions first. Call the tool FIRST, THEN talk."

LLMs in voice contexts tend to over-explain what they're about to do instead of just doing it. Your prompt needs to force immediate action.

### 3. Solve the "dead air" problem

The biggest failure mode in voice agents is silence. When a tool call takes 5-15 seconds, the user hears nothing and thinks something is broken.

The fix: **give the LLM specific content to talk about while waiting.**

Bad:
```
After calling search_hotels, chat with the caller while waiting.
```

Good:
```
WHILE WAITING FOR TOOL RESULTS:
- Do NOT ask questions — results arrive in 10-20s, the caller won't have time to answer
- Instead, SHARE destination facts from the knowledge below
- Save your questions for AFTER the results arrive

CITY KNOWLEDGE:
--- Paris (France) ---
Fun facts: The Eiffel Tower was supposed to be temporary...
Top attractions: Louvre Museum, Montmartre, Seine River cruise...
Current events: Paris Fashion Week runs March 3-11...
```

The key insight: **don't just tell the LLM to fill time — give it material to fill time with.** Embed facts, tips, trivia, or talking points directly in the prompt.

### 4. Repeat critical rules

In chat, you can state a rule once and the LLM follows it. In voice, the LLM is under more pressure (real-time, continuous) and tends to forget constraints. Repeat important rules 2-3 times in different sections.

From the hotel demo, "never fabricate" appears **three times**:

```
# In the base prompt:
NEVER FABRICATE DATA:
- NEVER make up hotel names, room types, prices...

# In the phase 2 prompt:
You CANNOT answer questions about rooms without calling get_hotel_details.
If you try to answer without calling the tool, you WILL fabricate data.

# In the tool description itself:
"Get detailed room information. You MUST call this before you can talk about rooms or prices."
```

### 5. Handle speech recognition errors in the prompt

STT is imperfect. Users say "four" and it transcribes "for". Users say phone numbers and they come through garbled. Your prompt needs to handle this.

From our banking demo:
```
DIGIT INTERPRETATION — CRITICAL:
Speech recognition garbles spoken digits. Your job is to interpret best digits.
- Map spoken words: 'for/fore/four' → 4, 'to/too/two' → 2
- Do NOT ask caller to repeat. Pick best interpretation and call the tool.
```

From the Spanish teacher demo:
```
The speech recognition may not be perfect for Spanish spoken by beginners.
- Be generous — if it sounds roughly close, count it as success
- Do NOT repeat back what the student said (transcription is imperfect)
```

### 6. Ban text-isms

LLMs trained on text naturally produce text patterns that sound terrible when spoken aloud:

```
SPEAKING STYLE:
- NEVER use action annotations like *smiles* or *typing*
- NEVER use bullet points or numbered lists — speak in natural sentences
- NEVER say "as you can see" or reference visual elements
- Keep responses to 2-3 sentences maximum
```

The sentence length constraint is critical. A chatbot response of 5-6 sentences is fine to read. Spoken aloud at 150 words/minute, it's a 30-second monologue where the user can't get a word in.

### 7. Design for deferred tool results

Some tools return instantly (what time is it?). Others take seconds (API calls, searches, database queries). The pattern for slow tools:

```python
async def handle_tool_call(tool_call, tool_handle):
    if tool_call.tool_name == "search_hotels":
        # Launch as background task — don't await
        asyncio.create_task(deferred_search(args, tool_handle))
        # The LLM keeps talking while this runs
```

And in your prompt, tell the LLM what to do during the wait:
```
After calling search_hotels, share fun facts about the destination.
The results will appear in 10-20 seconds. Keep the conversation going.
```

The tool result arrives asynchronously. The LLM gets it mid-sentence and incorporates it naturally.

### 8. Use multi-phase prompts for complex flows

For agents with multiple stages (search → select → book), swap the entire system prompt when the phase changes:

```python
# Phase 1: City selection
config = SessionConfig(instructions=get_phase1_prompt())

# User picks a city, tool returns results → swap prompt
new_config = SessionConfig(instructions=get_phase2_prompt(city, hotels))
await input_handle.send_config(new_config)

# User picks a hotel → swap again
new_config = SessionConfig(instructions=get_phase3_prompt(hotel, rooms))
await input_handle.send_config(new_config)
```

Each phase prompt has:
- A clear "YOUR ONE JOB RIGHT NOW" section
- Only the data relevant to that phase
- Explicit rules for transitioning to the next phase

This works much better than one giant prompt trying to cover all phases — the LLM stays focused.

### 9. Put instructions in tool descriptions too

The tool description isn't just for the user — it's instructions to the LLM about when and how to use the tool:

Bad:
```python
ToolDef(
    name="search_hotels",
    description="Search for hotels in a city",
    ...
)
```

Good:
```python
ToolDef(
    name="search_hotels",
    description="Search for available hotels in a city. Takes some time to query the system. Keep chatting with the caller about the destination while waiting for results!",
    ...
)
```

The description is part of the prompt. Use it.

## Connecting MCP servers

The `mcp_demo` in the repo shows how to connect any MCP server to a voice agent. It dynamically discovers tools via the MCP protocol and bridges them to gradbot.

The demo connects to filesystem and memory servers by default, configurable via `config.yaml`:

```yaml
mcp:
  servers:
    - name: filesystem
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "/tmp/workspace"]
    - name: memory
      command: npx
      args: ["-y", "@modelcontextprotocol/server-memory"]
    - name: fetch
      command: npx
      args: ["-y", "@modelcontextprotocol/server-fetch"]
```

Add a server, restart, and the voice agent automatically discovers and can use all its tools. No code changes needed.

## The demos

The repo includes 8 working demos, from simple to complex:

| Demo | Complexity | What it shows |
|------|-----------|---------------|
| `simple_chat` | Minimal | Basic voice conversation, no tools |
| `spanish_teacher` | Moderate | Language teaching, handling imperfect STT |
| `mtg_adviser` | Moderate | Domain knowledge (Magic: The Gathering cards) |
| `hotel` | Complex | Multi-phase booking flow with prompt swapping |
| `business_bank` | Complex | Multi-phase banking with digit interpretation |
| `fantasy_shop` | Complex | Character personas with persona switching |
| `voice_text_adventure` | Complex | Interactive fiction with state management |
| `mcp_demo` | Advanced | Dynamic MCP server integration |

Each demo follows the same structure: `main.py` (FastAPI + gradbot), `static/index.html` (browser UI), `pyproject.toml` (dependencies).

## Quick reference

```python
import gradbot

# Voices
voice = gradbot.flagship_voice("Emma")     # Get a specific voice
voices = gradbot.flagship_voices()          # List all voices

# Languages: Lang.En, Lang.Fr, Lang.Es, Lang.De, Lang.Pt

# Session
config = gradbot.SessionConfig(voice_id=..., instructions=..., language=..., tools=[...])
input_handle, output_handle = await gradbot.run(session_config=config, ...)

# Input
await input_handle.send_audio(bytes)          # Send audio
await input_handle.send_config(new_config)    # Update config mid-session
await input_handle.close()                    # End session

# Output (loop)
msg = await output_handle.receive()           # Returns None when done
msg.msg_type  # "audio" | "tts_text" | "stt_text" | "tool_call" | "event"
msg.data      # bytes (audio)
msg.text      # str (tts_text, stt_text)
msg.tool_call # ToolCallInfo (tool_call)
msg.tool_call_handle  # ToolCallHandlePy (tool_call)

# Tool results
await tool_handle.send(json_string)           # Success
await tool_handle.send_error(error_string)    # Error

# Audio formats: AudioFormat.OggOpus, AudioFormat.Pcm, AudioFormat.Ulaw
```
