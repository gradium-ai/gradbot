# Voice Text Adventure Demo

A voice-narrated text adventure demo using pygradbot and Jericho.

Speak commands to a voice AI narrator who plays classic Z-machine interactive fiction games (Zork, Enchanter, Planetfall, etc.) with you.

## Licensing Note

This demo depends on [Jericho](https://github.com/microsoft/jericho), which internally uses the Frotz Z-machine interpreter (GPL-licensed). If you distribute a bundle that includes the compiled Frotz interpreter (e.g., a Docker image), you'll need to comply with GPL terms for that component.

## Setup

```bash
cd gradbot/demos/voice_text_adventure
uv sync
```

> **After changing gradbot Rust code**, re-run with `uv sync --reinstall-package pygradbot` to rebuild the package.

### Download games

Jericho plays Z-machine game files (`.z3`, `.z5`, `.z8`, etc.). These are **not included** in this repo. Download them from the [BYU-PCCL Z-machine Games](https://github.com/BYU-PCCL/z-machine-games):

```bash
# From the voice_text_adventure directory:
wget https://github.com/BYU-PCCL/z-machine-games/archive/refs/heads/master.zip
unzip master.zip
cp z-machine-games-master/jericho-game-suite/*.z* games/
rm -rf z-machine-games-master master.zip
```

The `games/` directory should now contain `.z3`/`.z5`/`.z8` files.

### Download spaCy model

```bash
uv run python -m spacy download en_core_web_sm
```

## Run

```bash
# Set your API keys
export GRADIUM_API_KEY=your_key_here
export LLM_API_KEY=your_llm_key  # or use LLM_BASE_URL + LLM_API_KEY for other providers

# Run the server
uv run uvicorn main:app --reload
```

Then open http://localhost:8000 in your browser.

## Features

- Play 50+ classic text adventures by voice
- AI narrator reads game descriptions aloud with dramatic flair
- Speak commands naturally ("go north", "take the lamp")
- Multiple narrator styles (dramatic, spooky, comedic, mysterious, heroic)
- Multi-language narration (EN, FR, DE, ES, PT)
- Voice switching mid-game for dramatic effect
