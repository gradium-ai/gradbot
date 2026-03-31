# gradbot_filler Feature Guide

## Overview

The `gradbot_filler` tool allows the LLM to provide immediate filler content (like "Let me check that for you") while processing slower operations. This maintains conversation flow and makes the agent feel more responsive.

## Current Implementation Status

### ✅ What Works Now

1. **Immediate Detection During Streaming**: When the LLM calls `gradbot_filler(content="...")`, the system detects it **as soon as the JSON args are complete** during streaming (`llm.rs:785-809`)
2. **Instant Callback Invocation**: The callback is invoked immediately during streaming, not waiting for the stream to end
3. **Callback Wired Up**: The callback is automatically set at the multiplex layer (`multiplex.rs:204-208`)
4. **User-Controlled Result**: The user's tool handler sends the result (typically SUCCESS) to complete the tool call
5. **Comprehensive Logging**: Shows exactly when filler is detected during streaming

**Current Behavior**: The callback fires as soon as the filler content is available during streaming, providing maximum reactivity. The user's tool handler completes the tool call normally.

### 🚧 Full Immediate TTS (Future Enhancement)

Full immediate parallel TTS playback would require:
- Access to current `turn_idx` from the callback context
- Separate audio stream management to avoid turn filtering
- Additional complexity in the multiplex architecture

The callback hook is provided so you can implement immediate TTS at the application level if needed. The current multiplex callback just logs the filler content.

### 🎯 Current Benefits

- **Streaming Detection**: Callback fires as soon as args are complete, while LLM is still generating
- **Maximum Reactivity**: No waiting for stream to end before callback invocation
- **User Control**: You decide how to handle the tool call result
- **Clear Logging**: See exactly when detection happens during streaming
- **Simple Setup**: Just add tool definition and handle in your tool handler

## How to Use (Python Demos)

### 1. Add the Tool to Your Tools List

```python
tools = [
    gradbot.ToolDef(
        name="gradbot_filler",
        description="Provide filler content to speak immediately while waiting for other operations.",
        parameters_json=json.dumps({
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "Short filler phrase to speak"
                }
            },
            "required": ["content"]
        }),
    ),
    # ... other tools
]
```

### 2. Handle in Your Tool Call Handler (Required)

```python
async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
    if tool_call.tool_name == "gradbot_filler":
        content = json.loads(tool_call.args_json).get("content", "")
        logger.info(f"Filler: {content}")
        # You must send a result to complete the tool call
        # The callback already fired during streaming, this just completes it
        await tool_handle.send(json.dumps({"status": "SUCCESS"}))
```

**Important**: You must handle the tool call and send a result. The callback fires during streaming for immediate reactivity, but the tool call still needs to be completed by your handler.

### 3. Instruct the LLM in System Prompt

```python
system_prompt = """
TOOLS AVAILABLE:
- gradbot_filler: Use BEFORE slow operations to maintain conversation flow
  Examples: "Let me check", "One sec", "Sure thing"

USAGE PATTERN:
- Menu queries: gradbot_filler("Let me check that") + show_menu()
- Add items: gradbot_filler("Sure thing") + add_to_order()
- Multilingual: French: "Un instant", Spanish: "Un momento"
"""
```

## How It Works

```
┌─────────────────────────────────────────────────────────┐
│ 1. LLM starts streaming response                        │
│    Tool call chunks arrive incrementally                │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ 2. Streaming parser accumulates chunks (llm.rs:762-812)│
│    - Accumulates: id, name, args by index               │
│    - For each chunk: tries parsing args as JSON         │
│    - DETECTS: name=="gradbot_filler" + valid JSON       │
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼ (IMMEDIATE - during streaming!)
┌─────────────────────────────────────────────────────────┐
│ 3. Callback invoked IMMEDIATELY (llm.rs:799-801)       │
│    🗣️ "gradbot_filler detected during streaming"      │
│    callback(content)  ← fires while LLM still streaming!│
└────────────────┬────────────────────────────────────────┘
                 │
                 ▼
┌─────────────────────────────────────────────────────────┐
│ 4. Stream ends, tool call sent to client               │
│    - User handler receives gradbot_filler tool call     │
│    - User sends SUCCESS result to complete it           │
│    - Filler content spoken through normal TTS           │
└─────────────────────────────────────────────────────────┘
```

**Key Point**: The callback fires **during streaming** as soon as we have valid JSON args, not after the stream ends. This provides maximum reactivity - the callback can trigger immediate actions while the LLM is still generating the rest of its response.

## FAQ

### Q: Do I need to activate the filler_callback?
**A:** No! It's automatically activated when the Session is created at the multiplex layer. Just add the `gradbot_filler` tool to your tools list and the LLM can use it.

### Q: Are the Rust and Python tool definitions conflicting?
**A:** No! The automatic tool insertion has been removed from Rust. You just need to add the tool manually in your tools list (Python or Rust).

### Q: When does the callback fire?
**A:** The callback fires **during streaming** as soon as:
1. The tool name "gradbot_filler" is detected
2. The args JSON is complete and valid
3. A "content" field is present

This happens while the LLM is still generating its response, providing maximum reactivity. The log shows: `🗣️ gradbot_filler detected during streaming`

### Q: Do I need to handle the tool call result?
**A:** Yes! Unlike the previous implementation, you must handle the `gradbot_filler` tool call in your tool handler and send a result (typically SUCCESS). This gives you full control over completion.

### Q: Why not implement full immediate TTS?
**A:** Full immediate TTS would require managing separate audio streams with proper `turn_idx` handling to avoid conflicts with the main TTS flow. The current implementation provides the callback hook for you to implement immediate TTS at the application level if needed.

### Q: Can I use this in multiple languages?
**A:** Yes! Just provide language-appropriate filler phrases:
- English: "One sec", "Let me check"
- French: "Un instant", "Je vérifie"
- Spanish: "Un momento", "Déjame ver"
- German: "Einen Moment", "Lass mich schauen"
- Portuguese: "Só um momento", "Deixa eu ver"

## Logging

When `gradbot_filler` is called, you'll see these logs:

### Rust Layer (gradbot_lib) - During Streaming
```
# Detection happens DURING streaming as JSON becomes complete
INFO  🗣️ gradbot_filler detected during streaming idx=0 content="Let me check" has_callback=true
DEBUG invoking filler callback immediately
INFO  🎤 gradbot_filler called with content: 'Let me check'
INFO     Note: Immediate TTS callback received but not sending to TTS stream
INFO     (filler content will flow through normal LLM response)
```

### Python Layer (demo) - After Stream Ends
```
# Tool call arrives after streaming completes
INFO Tool call: gradbot_filler - {'content': 'Let me check'}
INFO 🗣️  gradbot_filler called with content: 'Let me check' (lang=en)
```

### What the Logs Tell You
- `detected during streaming`: Callback invoked while LLM still generating response!
- `idx=0`: The tool call index in the response
- `has_callback=true`: Callback is wired up and executed
- `invoking filler callback immediately`: Callback fired during streaming, not after
- Tool call still arrives at Python handler for result completion

## Example Implementation

See `demos/chick_fil_a/main.py` for a complete working example with:
- Tool definition (lines 288-301)
- System prompt instructions (lines 237-241, 263)
- Tool handler (lines 483-493)
- Comprehensive logging
