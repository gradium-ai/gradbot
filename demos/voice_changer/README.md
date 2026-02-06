# Voice Changer Demo

A real-time voice chat demo where the AI can switch between different voice personas mid-conversation.

## Features

- 14 flagship voices across 5 languages (English, French, German, Spanish, Portuguese)
- AI can switch voices using tool calls
- Each voice has a unique personality based on their description
- Real-time voice conversation with live transcripts
- Visual indicator showing current voice persona

## Setup

```bash
cd gradbot/demos/voice_changer
uv sync
```

This will build pygradbot from source using maturin.

## Run

```bash
# Set your API keys
export GRADIUM_API_KEY=your_key_here
export OPENAI_API_KEY=your_openai_key  # or use LLM_BASE_URL for other providers

# Run the server
uv run uvicorn main:app --reload
```

Then open http://localhost:8000 in your browser.

## How It Works

1. Select a starting voice persona
2. Start the conversation
3. Chat with the AI naturally
4. The AI will occasionally ask if you'd like to talk to someone else
5. When switching voices, the AI introduces itself as the new character

## Available Voices

### English (US)
- **Emma** - Warm and welcoming, friendly American accent
- **Kent** - Confident and professional, clear American accent

### English (UK)
- **Sydney** - Sophisticated and articulate, refined British accent
- **John** - Distinguished and authoritative, classic British accent
- **Eva** - Lively and engaging, dynamic British accent
- **Jack** - Relaxed and friendly, casual British accent

### French
- **Elise** - Charming and melodic, authentic French accent
- **Leo** - Smooth and charismatic, sophisticated French accent

### German
- **Mia** - Clear and precise, excellent for technical content
- **Maximilian** - Strong and reliable, authoritative German accent

### Spanish
- **Valentina** - Vibrant and expressive, Mexican Spanish accent
- **Sergio** - Warm and passionate, Castilian Spanish accent

### Portuguese
- **Alice** - Bright and cheerful, Brazilian Portuguese accent
- **Davi** - Calm and reassuring, Brazilian Portuguese accent
