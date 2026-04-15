# Frontend Guidelines for Gradbot Voice Agent Apps

The frontend is a single `static/index.html` file. Use the `frontend-design` skill for visual design, but ensure these technical requirements are met. These apply to both Path A (demos/) and Path B (standalone) - the WebSocket protocol and JS integration are identical.

## Required JavaScript Integration

### Audio Player Setup

The frontend MUST load the bundled gradbot JS library via script tags (NOT ES module imports). These files are served automatically by `gradbot.routes.setup()` at `/static/js/`. `SyncedAudioPlayer` is a global, not an ES module export.

CRITICAL: Use these three script tags BEFORE your main script. Do NOT use `import` or `type="module"`:

```html
<script src="/static/js/opus-encoder.js"></script>
<script src="/static/js/audio-processor.js"></script>
<script src="/static/js/synced-audio-player.js"></script>
<script>
  // Your app code here - SyncedAudioPlayer is available as a global
</script>
```

### WebSocket Connection Flow

```javascript
let ws = null;
let player = null;
let isRecording = false;

async function startCall() {
    // 1. Check audio config (PCM vs Opus)
    const audioConfig = await fetch('/api/audio-config').then(r => r.json());

    // 2. Initialize audio player
    player = new SyncedAudioPlayer({
        basePath: '/static/js',
        sampleRate: 24000,
        pcmOutput: audioConfig.pcm || false,
        echoCancellation: true,
        onEncodedAudio: (opusData) => {
            if (isRecording && ws?.readyState === WebSocket.OPEN) {
                ws.send(opusData);
            }
        },
        onText: ({ text, turnIdx, isUser }) => {
            // IMPORTANT: onText receives a SINGLE OBJECT, not separate args.
            // You must destructure it: ({ text, turnIdx, isUser })
            appendTranscript(text, turnIdx, isUser);
        },
        onEvent: (eventType, msg) => {
            handleCustomMessage(msg);
        },
    });

    await player.start();

    // 3. Connect WebSocket
    const protocol = location.protocol === 'https:' ? 'wss:' : 'ws:';
    const wsUrl = `${protocol}//${location.host}/ws/chat`;
    ws = new WebSocket(wsUrl);

    ws.onopen = () => {
        // 4. Send start message with any domain-specific params
        ws.send(JSON.stringify({
            type: 'start',
            speed: 1.0,
            // agent: 'Sophie',
            // language: 'en',
            // Any other start params your backend expects
        }));
        isRecording = true;
    };

    // 5. Route ALL messages through the player
    ws.onmessage = (event) => {
        player.handleMessage(event.data);
    };

    ws.onclose = () => endCall();
}

function endCall() {
    isRecording = false;
    if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: 'stop' }));
    }
    ws?.close();
    ws = null;
    player?.stop();
    player = null;
}
```

### Handling Custom Messages from Backend

The `onEvent` callback receives custom messages sent by `websocket.send_json()` in tool handlers:

```javascript
function handleCustomMessage(msg) {
    switch (msg.type) {
        case 'state_update':
            renderState(msg);
            break;
        case 'search_results':
            renderSearchResults(msg.results);
            break;
        case 'order_updated':
            renderOrder(msg.items, msg.total);
            break;
        case 'game_over':
            showGameOver(msg.winner, msg.score);
            break;
    }
}
```

### Transcript Display

CRITICAL: Text arrives incrementally — both user STT and agent TTS append word-by-word into the same bubble per turn. Use `turnIdx` to accumulate agent text. Use `hadAssistantBubble` flag to reuse the user bubble across multiple STT refinements. This is the exact pattern from the hotel demo — copy it verbatim:

```javascript
let turnBubbles = {};
let userBubble = null;
let hadAssistantBubble = false;

function getBubbleForTurn(turnIdx, isUser) {
    if (isUser) {
        // Reuse existing user bubble if agent hasn't spoken yet
        if (userBubble && !hadAssistantBubble) return userBubble;
        hadAssistantBubble = false;
        userBubble = document.createElement('div');
        userBubble.className = 'msg msg-user';
        const tx = document.createElement('span');
        tx.className = 'msg-text';
        userBubble.appendChild(tx);
        transcript.appendChild(userBubble);
        return userBubble;
    }
    let bubble = turnBubbles[turnIdx];
    if (!bubble) {
        hadAssistantBubble = true;
        bubble = document.createElement('div');
        bubble.className = 'msg msg-agent';
        const tx = document.createElement('span');
        tx.className = 'msg-text';
        bubble.appendChild(tx);
        transcript.appendChild(bubble);
        turnBubbles[turnIdx] = bubble;
    }
    return bubble;
}

function appendTranscript(text, turnIdx, isUser) {
    const bubble = getBubbleForTurn(turnIdx, isUser);
    // Always append — text streams in incrementally for both user and agent
    bubble.querySelector('.msg-text').textContent += text + ' ';
    // Garbage collect old bubbles
    while (transcript.children.length > 60) {
        const removed = transcript.removeChild(transcript.firstChild);
        for (const k in turnBubbles) { if (turnBubbles[k] === removed) delete turnBubbles[k]; }
        if (userBubble === removed) userBubble = null;
    }
    transcript.scrollTop = transcript.scrollHeight;
}
```

### Mid-Session Config Changes (e.g., speed slider)

```javascript
speedSlider.addEventListener('input', () => {
    if (ws?.readyState === WebSocket.OPEN && isRecording) {
        ws.send(JSON.stringify({
            type: 'config',
            speed: parseFloat(speedSlider.value),
        }));
    }
});
```

## Required UI Elements

Every gradbot voice agent frontend MUST include:

1. **Call/mic button** - Toggles `startCall()` / `endCall()`
2. **Transcript area** - Shows user (right-aligned) and agent (left-aligned) messages
3. **Connection status** - "Connecting...", "Connected", "Call ended"
4. **Speed control** - Slider 0.5x to 2.0x (sends config message)
5. **Echo cancellation toggle** - Checkbox (checked by default). Without this, the agent hears its own TTS output and enters a feedback loop. Wire it to `SyncedAudioPlayer`:

```html
<label><input type="checkbox" id="echoCancellation" checked> Echo cancellation</label>
```
```javascript
// In SyncedAudioPlayer config:
echoCancellation: document.getElementById('echoCancellation').checked,
```

## Optional UI Elements (domain-specific)

- **Menu/inventory display** - For ordering or game apps
- **Search results panel** - For search-based agents
- **Score/progress tracker** - For games or tutoring
- **Agent/voice selector** - Dropdown or buttons
- **Language selector** - For multilingual apps

## Layout Patterns

### Two-Panel (search/browse + chat)
Left: search results, menu, or content. Right: transcript and controls.

### Single Panel with Overlay (games/simple agents)
Main content area with floating transcript and mic button.

### Three-Column (ordering with menu + cart)
Left: content/menu. Center: agent/transcript. Right: cart/state.

## Design Notes for frontend-design Skill

When invoking the frontend-design skill, include these in the prompt:
- "This is a voice agent UI - the primary interaction is speech, not typing"
- "Include a prominent mic/call button as the main CTA"
- "The transcript should be visible but secondary to the domain content"
- "Design for the specific domain" (e.g., "a medieval fantasy shop", "a luxury hotel concierge")
- "Must be a single HTML file with embedded CSS and JS"
- "Load SyncedAudioPlayer via `<script src='/static/js/synced-audio-player.js'>` (it's a global, NOT an ES module)"
- Specify what custom message types the frontend needs to handle
- "Include a speed slider (0.5x - 2.0x) that sends WebSocket config messages"

## WebSocket Protocol Summary

**Client sends:**
| Message | Format |
|---------|--------|
| Start session | `{"type": "start", "speed": 1.0, ...}` |
| Audio frames | Binary (Opus-encoded) |
| Config change | `{"type": "config", "speed": 1.5}` |
| End session | `{"type": "stop"}` |

**Server sends (handled by SyncedAudioPlayer):**
| Message | Format |
|---------|--------|
| Audio timing | `{"type": "audio_timing", "start_s", "stop_s", "turn_idx", "interrupted"}` |
| Audio data | Binary (Opus or PCM) |
| Agent text | `{"type": "transcript", "text", "is_user": false, "turn_idx"}` |
| User text | `{"type": "transcript", "text", "is_user": true}` |
| Event | `{"type": "event", "event": "end_of_turn"}` |

**Server sends (custom, handled by onEvent):**
| Message | Format |
|---------|--------|
| Any custom | `{"type": "your_custom_type", ...}` |

The SyncedAudioPlayer handles audio_timing, binary audio, and transcript messages automatically. Custom JSON messages (any type not in the standard set) are passed to the `onEvent` callback.
