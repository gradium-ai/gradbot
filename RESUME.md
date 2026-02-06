# Resume: LLM Function Calls & Dynamic Prompt Updates

## Completed

- Refactored `LlmConfig` with `user_prompt` and `language` fields
- `push()` now takes `Arc<LlmConfig>` with `ptr_eq` optimization via `maybe_update()`
- Removed `set_prompt()` - config is passed through `push()`
- Removed `Instructions` enum - prompts are plain strings
- `SessionConfig` and call sites updated (twilio, openai servers)

## Next Steps

### 1. Add tools to LlmConfig

```rust
pub struct LlmConfig {
    pub user_prompt: String,
    pub language: crate::system_prompt::Lang,
    pub tools: Vec<Tool>,  // Add this
}
```

### 2. Handle config changes mid-conversation

Instead of erroring when config changes after messages exist, inject a special user message:

```
[NEW SYSTEM PROMPT]
<new instructions here>
```

Update `push()` logic:
- If `ptr_eq` returns false AND messages not empty -> inject update message
- If messages empty -> set system prompt as before

### 3. Update system_prompt.rs

Add meta-instructions to the system prompt template:

```
Your instructions can be updated during the conversation.
When you see [NEW SYSTEM PROMPT], treat the following text as your updated instructions.
```

Consider also:
- `[ADDITIONAL INSTRUCTIONS]` - additive updates
- `[TOOL RESULT: tool_name]` - function call responses

### 4. Tool execution flow

For now: pause TTS output while tool executes, then resume.

Future: MCP-like pattern where LLM chitchats while waiting for tool results.

### 5. Self-summarization for context

When context gets long or on major prompt changes:
- Ask LLM to summarize conversation so far
- Start fresh with new system prompt + summary
- Works universally across all models

## Files touched

- `src/llm.rs` - LlmConfig, push() signature
- `src/multiplex.rs` - SessionConfig, llm_tts()
- `src/system_prompt.rs` - will need meta-instructions
- `src/lib.rs` - TwilioConfig
- `src/openai_server.rs` - config parsing
- `src/twilio_server.rs` - config construction
