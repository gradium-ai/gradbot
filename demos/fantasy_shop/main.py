"""
Fantasy Shop Demo - A haggling game with voice AI

A text adventure style game where you haggle for a sword in a fantasy shop.

Run with: uvicorn main:app --reload
"""

import asyncio
import json
import logging
import os
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import gradbot
from gradbot.fastapi import websocket_chat_handler

# Initialize Rust logging
gradbot.init_logging()

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

logger = logging.getLogger(__name__)


@dataclass
class GameState:
    """Tracks the current state of the game."""
    gold: int = 100
    has_fake_ruby: bool = True
    sword_price: int = 150
    discount_applied: bool = False
    ruby_given: bool = False
    current_character: str = "attendant"  # "attendant" or "manager"
    character_name: str = "Grumbold"  # Current character's name
    game_over: bool = False
    game_won: bool = False
    language: str = "en"  # Current language code


LANG_MAP = {
    "en": gradbot.Lang.En,
    "fr": gradbot.Lang.Fr,
    "de": gradbot.Lang.De,
    "es": gradbot.Lang.Es,
    "pt": gradbot.Lang.Pt,
}

LANG_NAMES = {
    "en": "English",
    "fr": "French",
    "de": "German",
    "es": "Spanish",
    "pt": "Portuguese",
}

# Voice mappings by language and gender
# Format: (language_code, gender) -> (voice_name, character_name_suffix)
VOICE_MAP = {
    # Attendant voices (masculine)
    ("en", "masculine"): ("Jack", "Grumbold"),
    ("fr", "masculine"): ("Leo", "Guillaume"),
    ("de", "masculine"): ("Maximilian", "Heinrich"),
    ("es", "masculine"): ("Sergio", "Fernando"),
    ("pt", "masculine"): ("Davi", "Roberto"),
    # Manager voices (feminine)
    ("en", "feminine"): ("Sydney", "Princess Celestia"),
    ("fr", "feminine"): ("Elise", "Princesse Celestine"),
    ("de", "feminine"): ("Mia", "Prinzessin Celestia"),
    ("es", "feminine"): ("Valentina", "Princesa Celestina"),
    ("pt", "feminine"): ("Alice", "Princesa Celestina"),
}


def get_voice_for_role(language: str, role: str) -> tuple[gradbot.FlagshipVoice, str]:
    """Get the appropriate voice and character name for a role in a language.

    Args:
        language: Language code (en, fr, de, es, pt)
        role: Either "attendant" (masculine) or "manager" (feminine)

    Returns:
        Tuple of (FlagshipVoice, character_name)
    """
    gender = "masculine" if role == "attendant" else "feminine"
    voice_name, char_name = VOICE_MAP.get((language, gender), VOICE_MAP[("en", gender)])
    voice = gradbot.flagship_voice(voice_name)
    return voice, char_name


_PROMPTS_DIR = Path(__file__).parent / "prompts"
_ATTENDANT_PROMPT_TEMPLATE = (_PROMPTS_DIR / "attendant.txt").read_text()
_MANAGER_PROMPT_TEMPLATE = (_PROMPTS_DIR / "manager.txt").read_text()


def get_attendant_prompt(state: GameState, char_name: str = "Grumbold") -> str:
    """System prompt for the shop attendant."""
    lang_name = LANG_NAMES.get(state.language, "English")
    lang_instruction = f"\n\nIMPORTANT: Speak in {lang_name}!\n"

    return _ATTENDANT_PROMPT_TEMPLATE.format(
        char_name=char_name,
        lang_instruction=lang_instruction,
        sword_price=state.sword_price,
        gold=state.gold,
    )


def get_manager_prompt(state: GameState, char_name: str = "Princess Celestia") -> str:
    """System prompt for the manager (secretly a princess)."""
    price_info = f"Current price: {state.sword_price} gold"
    if state.discount_applied:
        price_info += " (discount already applied)"

    lang_name = LANG_NAMES.get(state.language, "English")
    lang_instruction = f"\n\nIMPORTANT: Speak in {lang_name}!\n"

    ruby_status = (
        "The customer no longer has the ruby (they gave it to you earlier)"
        if state.ruby_given
        else "The customer has a gemstone that might be valuable"
    )

    discount_status = (
        "You've already applied the formal discount. But you can still adjust the price if moved by generosity."
        if state.discount_applied
        else "You haven't applied any discount yet."
    )

    return _MANAGER_PROMPT_TEMPLATE.format(
        char_name=char_name,
        lang_instruction=lang_instruction,
        price_info=price_info,
        gold=state.gold,
        ruby_status=ruby_status,
        discount_status=discount_status,
    )


def build_language_tool() -> gradbot.ToolDef:
    """Tool for changing language when customer speaks another language."""
    return gradbot.ToolDef(
        name="change_language",
        description="Change the conversation language when the customer speaks in a different language. Supported: en (English), fr (French), de (German), es (Spanish), pt (Portuguese).",
        parameters_json=json.dumps({
            "type": "object",
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["en", "fr", "de", "es", "pt"],
                    "description": "The language code to switch to"
                }
            },
            "required": ["language"]
        }),
    )


def build_attendant_tools() -> list[gradbot.ToolDef]:
    """Tools available to the shop attendant."""
    return [
        build_language_tool(),
        gradbot.ToolDef(
            name="get_sword_price",
            description="Check the current price of the sword. Call this to see the current price after any changes.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        gradbot.ToolDef(
            name="kick_out_of_shop",
            description="Kick the customer out of the shop. Use when they try to scam you with fake gems or become abusive. This ends the game.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why you're kicking them out"
                    }
                },
                "required": ["reason"]
            }),
        ),
        gradbot.ToolDef(
            name="call_manager",
            description="Call the shop manager to handle a special discount request. Use when the customer needs a bigger discount than you can offer.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why you're calling the manager"
                    }
                },
                "required": ["reason"]
            }),
        ),
        gradbot.ToolDef(
            name="apply_discount",
            description="Try to apply a discount to the sword. As attendant, you can TRY but may not have authority.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why they deserve the discount"
                    }
                },
                "required": ["reason"]
            }),
        ),
        gradbot.ToolDef(
            name="sell_sword",
            description="Complete the sale of the sword to the customer. Use when the customer agrees to buy at the current price and has enough gold.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "final_price": {
                        "type": "integer",
                        "description": "The agreed price for the sword"
                    }
                },
                "required": ["final_price"]
            }),
        ),
    ]


def build_manager_tools() -> list[gradbot.ToolDef]:
    """Tools available to the manager."""
    return [
        build_language_tool(),
        gradbot.ToolDef(
            name="get_sword_price",
            description="Check the current price of the sword. Call this to see the current price after any changes.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        gradbot.ToolDef(
            name="kick_out_of_shop",
            description="Kick the customer out of the shop. Use when their intentions for the sword are unworthy (selfish, harmful). This ends the game.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why you're kicking them out"
                    }
                },
                "required": ["reason"]
            }),
        ),
        gradbot.ToolDef(
            name="apply_discount",
            description="Apply a 25 gold coin discount to the sword. Use ONLY when convinced the customer will use the sword to defend the village or fight dragons.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why they deserve the discount"
                    }
                },
                "required": ["reason"]
            }),
        ),
        gradbot.ToolDef(
            name="sell_sword",
            description="Complete the sale of the sword to the customer. Use when the customer agrees to buy at the current price and has enough gold.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "final_price": {
                        "type": "integer",
                        "description": "The agreed price for the sword"
                    }
                },
                "required": ["final_price"]
            }),
        ),
        gradbot.ToolDef(
            name="accept_ruby_gift",
            description="Accept the ruby as a gift from the customer and give them a 25 gold discount in return. Use when the customer offers you their ruby/gem as a gift (not as payment). This removes the ruby from their inventory and reduces the sword price.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
    ]


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting Fantasy Shop Demo...")
    yield
    logger.info("Shutting down...")


app = FastAPI(title="Fantasy Shop Demo", lifespan=lifespan)


@app.get("/api/game-info")
async def game_info():
    """Return initial game information."""
    return JSONResponse(content={
        "title": "The Sharp Edge - Fantasy Weapon Shop",
        "goal": "Buy the legendary sword Dragonbane",
        "starting_gold": 100,
        "sword_price": 150,
        "inventory": ["100 gold coins", "Fake ruby (looks real!)"],
    })


@app.websocket("/ws/game")
async def websocket_game(websocket: WebSocket):
    """WebSocket endpoint for the game."""

    # Per-session state
    state = GameState()
    attendant_voice, state.character_name = get_voice_for_role(state.language, "attendant")
    tools = build_attendant_tools()

    async def on_start(msg: dict) -> gradbot.SessionConfig:
        logger.info("Starting fantasy shop game")

        # Send initial game state
        await websocket.send_json({
            "type": "game_state",
            "state": {
                "gold": state.gold,
                "has_fake_ruby": state.has_fake_ruby,
                "sword_price": state.sword_price,
                "current_character": state.current_character,
                "character_name": f"{state.character_name} the Attendant",
            }
        })

        return gradbot.SessionConfig(
            voice_id=attendant_voice.voice_id,
            instructions=get_attendant_prompt(state, state.character_name),
            language=LANG_MAP[state.language],
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                rewrite_rules=LANG_MAP[state.language].rewrite_rules,
                assistant_speaks_first=True,
            ),
        )

    async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
        """Handle game tool calls."""
        nonlocal tools

        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        reason = args.get("reason", "No reason given")

        logger.info("Tool call: %s - %s", tool_name, reason)

        if tool_name == "get_sword_price":
            await tool_handle.send(json.dumps({
                "current_price": state.sword_price,
                "customer_gold": state.gold,
                "can_afford": state.gold >= state.sword_price,
            }))

        elif tool_name == "kick_out_of_shop":
            state.game_over = True
            await websocket.send_json({
                "type": "game_over",
                "reason": reason,
                "won": False,
            })
            await tool_handle.send(json.dumps({"result": "Customer has been kicked out"}))

        elif tool_name == "call_manager":
            # DEFERRED: manager takes 10 seconds to arrive
            await asyncio.sleep(10)

            state.current_character = "manager"
            tools = build_manager_tools()

            # Get manager voice for current language
            manager_voice, state.character_name = get_voice_for_role(state.language, "manager")

            # Update to manager
            new_config = gradbot.SessionConfig(
                voice_id=manager_voice.voice_id,
                instructions=get_manager_prompt(state, state.character_name),
                language=LANG_MAP[state.language],
                tools=tools,
                **merge_overrides(_OVERRIDES,
                    flush_duration_s=FLUSH_FOR_S,
                    rewrite_rules=LANG_MAP[state.language].rewrite_rules,
                ),
            )
            await input_handle.send_config(new_config)

            display_name = f"The Manager ({state.character_name})"
            await websocket.send_json({
                "type": "character_change",
                "character": "manager",
                "character_name": display_name,
            })
            await websocket.send_json({
                "type": "game_state",
                "state": {
                    "gold": state.gold,
                    "has_fake_ruby": state.has_fake_ruby,
                    "sword_price": state.sword_price,
                    "current_character": state.current_character,
                    "character_name": display_name,
                }
            })
            await tool_handle.send(json.dumps({
                "result": f"PERSONA CHANGE: The attendant has left the room. You are now {state.character_name}, the manager. Do NOT continue speaking as the attendant. Greet the customer as {state.character_name}."
            }))

        elif tool_name == "change_language":
            new_lang = args.get("language", "en")
            if new_lang in LANG_MAP:
                old_char_name = state.character_name
                state.language = new_lang
                lang_name = LANG_NAMES.get(new_lang, "English")

                # Call a colleague who speaks the new language (same role/gender)
                new_voice, state.character_name = get_voice_for_role(new_lang, state.current_character)

                # Get the appropriate prompt with new character name
                if state.current_character == "manager":
                    prompt = get_manager_prompt(state, state.character_name)
                    display_name = f"The Manager ({state.character_name})"
                    role_desc = "manager"
                else:
                    prompt = get_attendant_prompt(state, state.character_name)
                    display_name = f"{state.character_name} the Attendant"
                    role_desc = "attendant"

                # Update session with new voice and language
                new_config = gradbot.SessionConfig(
                    voice_id=new_voice.voice_id,
                    instructions=prompt,
                    language=LANG_MAP[new_lang],
                    tools=tools,
                    **merge_overrides(_OVERRIDES,
                        flush_duration_s=FLUSH_FOR_S,
                        rewrite_rules=LANG_MAP[new_lang].rewrite_rules,
                    ),
                )
                await input_handle.send_config(new_config)

                # Notify about the colleague change
                await websocket.send_json({
                    "type": "character_change",
                    "character": state.current_character,
                    "character_name": display_name,
                })
                await websocket.send_json({
                    "type": "game_state",
                    "state": {
                        "gold": state.gold,
                        "has_fake_ruby": state.has_fake_ruby,
                        "sword_price": state.sword_price,
                        "current_character": state.current_character,
                        "character_name": display_name,
                    }
                })
                await websocket.send_json({
                    "type": "game_event",
                    "event": "language_change",
                    "message": f"{old_char_name} called their {lang_name}-speaking colleague {state.character_name}",
                })
                await tool_handle.send(json.dumps({
                    "result": f"You called your {lang_name}-speaking colleague {state.character_name} to take over. You are now {state.character_name}, the {role_desc}. Greet the customer in {lang_name} and continue helping them. You know the previous conversation context.",
                    "language": new_lang,
                    "new_character": state.character_name,
                }))
            else:
                await tool_handle.send_error(f"Unknown language: {new_lang}")

        elif tool_name == "apply_discount":
            # Only manager can apply discounts!
            if state.current_character != "manager":
                await tool_handle.send(json.dumps({
                    "result": "FAILED: You don't have authority to apply discounts! Only the manager can do that. You tried your best but the system rejected it. Apologize to the customer and suggest calling the manager.",
                    "success": False,
                }))
            elif state.discount_applied:
                await tool_handle.send(json.dumps({
                    "result": "Discount was already applied earlier.",
                    "new_price": state.sword_price,
                }))
            else:
                state.discount_applied = True
                state.sword_price -= 25

                display_name = f"The Manager ({state.character_name})"
                await websocket.send_json({
                    "type": "game_state",
                    "state": {
                        "gold": state.gold,
                        "has_fake_ruby": state.has_fake_ruby,
                        "sword_price": state.sword_price,
                        "current_character": state.current_character,
                        "character_name": display_name,
                        "discount_applied": True,
                    }
                })

                # Check if player can now afford it
                if state.gold >= state.sword_price:
                    await websocket.send_json({
                        "type": "game_event",
                        "event": "can_afford",
                        "message": "You can now afford the sword!",
                    })

                await tool_handle.send(json.dumps({
                    "result": f"Discount applied! New price is {state.sword_price} gold coins.",
                    "new_price": state.sword_price,
                }))

        elif tool_name == "sell_sword":
            final_price = args.get("final_price", state.sword_price)

            if state.gold >= final_price:
                state.gold -= final_price
                state.game_won = True
                state.game_over = True

                await websocket.send_json({
                    "type": "game_won",
                    "final_price": final_price,
                    "message": "You acquired the legendary sword Dragonbane!",
                })
                await tool_handle.send(json.dumps({
                    "result": "Sale complete! The customer now owns Dragonbane!",
                    "success": True,
                }))
            else:
                await tool_handle.send(json.dumps({
                    "result": f"Customer doesn't have enough gold. They have {state.gold} but need {final_price}.",
                    "success": False,
                }))

        elif tool_name == "accept_ruby_gift":
            if not state.has_fake_ruby:
                await tool_handle.send(json.dumps({
                    "result": "The customer doesn't have a ruby to give.",
                    "success": False,
                }))
            elif state.ruby_given:
                await tool_handle.send(json.dumps({
                    "result": "You already accepted the ruby earlier.",
                    "success": False,
                }))
            else:
                state.has_fake_ruby = False
                state.ruby_given = True
                state.sword_price -= 25

                if state.current_character == "manager":
                    display_name = f"The Manager ({state.character_name})"
                else:
                    display_name = f"{state.character_name} the Attendant"

                await websocket.send_json({
                    "type": "game_state",
                    "state": {
                        "gold": state.gold,
                        "has_fake_ruby": state.has_fake_ruby,
                        "sword_price": state.sword_price,
                        "current_character": state.current_character,
                        "character_name": display_name,
                        "ruby_given": True,
                    }
                })

                # Check if player can now afford it
                if state.gold >= state.sword_price:
                    await websocket.send_json({
                        "type": "game_event",
                        "event": "can_afford",
                        "message": "You can now afford the sword!",
                    })

                await tool_handle.send(json.dumps({
                    "result": f"You graciously accepted the ruby as a gift and reduced the price by 25 gold. New price: {state.sword_price} gold.",
                    "new_price": state.sword_price,
                    "success": True,
                }))

        else:
            await tool_handle.send_error(f"Unknown tool: {tool_name}")

    await websocket_chat_handler(
        websocket,
        on_start=on_start,
        on_tool_call=handle_tool_call,
        run_kwargs=_CLIENT_CONFIG,
        output_format=gradbot.AudioFormat.Pcm if USE_PCM else gradbot.AudioFormat.OggOpus,
        debug=DEBUG,
    )


@app.get("/api/audio-config")
async def audio_config():
    return JSONResponse(content={"pcm": USE_PCM})

# Serve static files
static_dir = Path(__file__).parent / "static"
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=static_dir, follow_symlink=True), name="static")


@app.get("/")
async def index():
    """Serve the main page."""
    index_path = Path(__file__).parent / "static" / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse(
        content={"error": "Frontend not found. Place index.html in static/"},
        status_code=404
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
