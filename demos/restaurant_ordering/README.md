# Restaurant Ordering Demo

A voice AI ordering agent that helps customers browse the menu, customize items, and place fast-food orders.

## Features

- **Voice Interaction**: Speak naturally to browse the menu and place orders
- **Menu Categories**: Entrees, sides, drinks, and desserts
- **Item Customization**: Choose bread types, sauces, toppings, and more
- **Order Management**: View current order, modify items, and see the total
- **Natural Conversation**: The agent asks clarifying questions and makes suggestions
- **Multilingual**: English, French, German, Spanish, Portuguese

## Setup

1. Set your API keys:
```bash
export GRADIUM_API_KEY=your_gradium_key
export LLM_API_KEY=your_llm_key      # Any OpenAI-compatible API
export LLM_BASE_URL=...              # Optional, for custom endpoints
```

2. Install dependencies:
```bash
uv sync
```

3. Run the server:
```bash
uv run uvicorn main:app --reload
```

4. Open your browser to http://localhost:8000

## Usage

1. Click "Start Ordering" to begin
2. Say things like:
   - "Can I see the menu?"
   - "I'd like a spicy chicken sandwich"
   - "Add waffle fries, large size"
   - "What drinks do you have?"
   - "I'll have a lemonade"
   - "What's my total?"
   - "I'm ready to checkout"

## How It Works

The demo uses the gradbot library to connect:
- **STT**: Gradium speech-to-text for voice input
- **LLM**: OpenAI-compatible API for conversation
- **TTS**: Gradium text-to-speech for voice output

The agent has access to tools for:
- `show_menu`: Display menu categories
- `add_to_order`: Add items with customizations
- `view_order`: Show current order and total
- `remove_from_order`: Remove items
- `place_order`: Finalize the order

## Customization

Edit `menu.json` to:
- Add/remove menu items
- Change prices
- Modify customization options
- Add new categories

Edit the system prompt in `main.py` to change the agent's personality and behavior.
