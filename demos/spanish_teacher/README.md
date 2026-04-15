# Spanish Teacher Demo

A voice-based Spanish language learning demo where an AI teacher (Valentina from Mexico) teaches you Spanish sentences.

## Features

- Learn 6 Spanish sentences with pronunciation practice
- Teacher explains overall meaning, then breaks down each word
- Voice-based interaction - repeat after the teacher
- Progress tracking with success/failure counts
- Tolerant of imperfect pronunciation (STT is not shown to avoid confusion)

## Setup

```bash
cd gradbot/demos/spanish_teacher
uv sync
```

This will build gradbot from source using maturin.

> **After changing gradbot Rust code**, re-run with `uv sync --reinstall-package gradbot` to rebuild the package. A plain `uv sync` won't pick up changes if the version hasn't changed.

## Run

```bash
# Set your API keys
export GRADIUM_API_KEY=your_key_here
export LLM_API_KEY=your_llm_key  # or use LLM_BASE_URL + LLM_API_KEY for other providers

# Run the server
uv run uvicorn main:app --reload
```

Then open http://localhost:8000 in your browser.

## How It Works

1. Click "Start Lesson" to begin
2. Valentina introduces herself and starts teaching
3. For each sentence, she:
   - Explains the overall meaning in English
   - Goes through each word one by one
   - Says the complete sentence for you to repeat
4. Try to repeat the sentence after her
5. She'll determine if you were close enough and move on or encourage another try
6. Complete all 6 sentences to finish the lesson!

## Teaching Method

The teacher uses a word-by-word breakdown approach:
- First explains what the sentence means
- Then teaches each word individually with its meaning
- Finally has you practice the complete sentence

## Note on Speech Recognition

The demo intentionally does not display what you said because:
- Speech recognition for non-native Spanish speakers is imperfect
- Seeing incorrect transcriptions could be discouraging
- The AI teacher is prompted to be generous in accepting attempts
