# Fantasy Shop Demo

A voice-based haggling game set in a fantasy weapon shop. Use your wits (and a fake ruby) to acquire the legendary sword Dragonbane!

## The Challenge

- **Goal**: Buy the sword Dragonbane (costs 150 gold)
- **You have**: 100 gold coins + a fake ruby
- **The gap**: You need to haggle, charm, or trick your way to a discount!

## Characters

### Grumbold (Shop Attendant)
- Can haggle but won't go below 140 gold
- Will kick you out if you try to sell him the fake ruby
- Can call the manager for bigger discounts

### Princess Celestia (The Manager)
- Secretly a princess checking on her kingdom's merchants
- Can apply a 25 gold discount, but only for a convincing worthy cause
- Will kick you out if your intentions are selfish
- May also accept the ruby as a heartfelt gift if you do not treat it as payment

## Winning Strategies

1. **The Hero's Path**: Convince the manager you need the sword to fight dragons
2. **The Gift Path**: Offer the ruby as a sincere gift, not as payment, and downplay its value
3. **Combination**: Use both approaches for maximum discount!

## Setup

```bash
cd gradbot/demos/fantasy_shop
uv sync
```

> **After changing gradbot Rust code**, re-run with `uv sync --reinstall-package gradbot` to rebuild the package.

## Run

```bash
export GRADIUM_API_KEY=your_key_here
export LLM_API_KEY=your_llm_key
export LLM_BASE_URL=your_llm_endpoint

uv run uvicorn main:app --reload
```

Open http://localhost:8000

## Game Tools

The AI characters have access to these tools:

- `get_sword_price` - Check current sword price
- `kick_out_of_shop` - Game over!
- `call_manager` - Summon the manager (attendant only)
- `apply_discount` - Apply 25 gold discount (manager only, requires worthy cause)
- `accept_ruby_gift` - Accept the ruby as a gift and reduce the price
- `sell_sword` - Complete the purchase (victory!)
- `change_language` - Switch to French, German, Spanish, or Portuguese during the conversation
