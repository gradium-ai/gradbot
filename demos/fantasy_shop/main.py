"""
Fantasy Shop Demo - A haggling game with voice AI

A text adventure style game where you haggle for a sword in a fantasy shop.

Run with: uvicorn main:app --reload
"""

import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

import pygradbot

# Initialize Rust logging
pygradbot.init_logging()


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
    "en": pygradbot.Lang.En,
    "fr": pygradbot.Lang.Fr,
    "de": pygradbot.Lang.De,
    "es": pygradbot.Lang.Es,
    "pt": pygradbot.Lang.Pt,
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
    ("en", "masculine"): ("Kent", "Grumbold"),
    ("fr", "masculine"): ("Leo", "Guillaume"),
    ("de", "masculine"): ("Maximilian", "Heinrich"),
    ("es", "masculine"): ("Sergio", "Fernando"),
    ("pt", "masculine"): ("Davi", "Roberto"),
    # Manager voices (feminine)
    ("en", "feminine"): ("Eva", "Princess Celestia"),
    ("fr", "feminine"): ("Elise", "Princesse Célestine"),
    ("de", "feminine"): ("Mia", "Prinzessin Celestia"),
    ("es", "feminine"): ("Valentina", "Princesa Celestina"),
    ("pt", "feminine"): ("Alice", "Princesa Celestina"),
}


def get_voice_for_role(language: str, role: str) -> tuple[pygradbot.FlagshipVoice, str]:
    """Get the appropriate voice and character name for a role in a language.

    Args:
        language: Language code (en, fr, de, es, pt)
        role: Either "attendant" (masculine) or "manager" (feminine)

    Returns:
        Tuple of (FlagshipVoice, character_name)
    """
    gender = "masculine" if role == "attendant" else "feminine"
    voice_name, char_name = VOICE_MAP.get((language, gender), VOICE_MAP[("en", gender)])
    voice = pygradbot.flagship_voice(voice_name)
    return voice, char_name


def get_attendant_prompt(state: GameState, char_name: str = "Grumbold") -> str:
    """System prompt for the shop attendant."""
    lang_name = LANG_NAMES.get(state.language, "English")
    lang_instruction = f"\n\nIMPORTANT: Speak in {lang_name}!\n"

    return f"""You are {char_name}, a gruff but fair shop attendant in a fantasy weapon shop called "The Sharp Edge".{lang_instruction}

CURRENT SITUATION:
- The customer wants to buy the legendary sword "Dragonbane"
- The sword is priced at {state.sword_price} gold coins
- You can haggle and reduce the price, but NEVER go below 140 gold coins
- The customer has {state.gold} gold coins (you can sense this magically)
- The customer also has a gemstone in their pocket

YOUR PERSONALITY:
- You're a seasoned merchant who enjoys a good haggle
- You're gruff but ultimately want to make a sale
- Add vivid details about the shop, the sword's history, or your merchant life

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *sighs* or *dramatic pause* - just speak naturally
- Let your voice convey emotion, don't describe your actions

IMPORTANT RULES:
1. If the customer offers a fair price (140+ gold), accept it enthusiastically
2. If they try to go below 140, refuse but stay friendly - hint that maybe the manager could help with a bigger discount
3. If the customer tries to SELL you a gem or ruby, be suspicious! Examine it and if they insist, call kick_out_of_shop because it's clearly fake
4. After some haggling (3-4 exchanges), suggest they might want to speak to the manager for special discounts - use call_manager

TOOLS:
- kick_out_of_shop: Use ONLY if the customer tries to pass off the fake ruby as payment or becomes abusive
- call_manager: Use when the customer wants a bigger discount than you can offer, or asks to speak to management
- apply_discount: You can TRY to apply a discount, but you don't have the authority - only the manager does!
- sell_sword: Use when the customer agrees to buy the sword and has enough gold. This completes the sale!
- change_language: If the customer speaks in French, German, Spanish, or Portuguese, switch to their language!

LANGUAGE: If the customer speaks in another language, call change_language to switch. You're multilingual!

Start by greeting the customer and asking how you can help them today.
"""


def get_manager_prompt(state: GameState, char_name: str = "Princess Celestia") -> str:
    """System prompt for the manager (secretly a princess)."""
    price_info = f"Current price: {state.sword_price} gold"
    if state.discount_applied:
        price_info += " (discount already applied)"

    lang_name = LANG_NAMES.get(state.language, "English")
    lang_instruction = f"\n\nIMPORTANT: Speak in {lang_name}!\n"

    return f"""You are {char_name}, disguised as the shop manager. You're secretly checking on your kingdom's merchants.{lang_instruction}

CURRENT SITUATION:
- A customer wants to buy the legendary sword "Dragonbane"
- {price_info}
- The customer has {state.gold} gold coins
- {"The customer no longer has the ruby (they gave it to you earlier)" if state.ruby_given else "The customer has a gemstone that might be valuable"}

YOUR PERSONALITY:
- You speak with hidden elegance that occasionally slips through
- You're kind but wise - you want the sword to go to a worthy hero
- Add evocative details about the sword's legend or the kingdom's needs

SPEAKING STYLE:
- Keep responses to 2-3 sentences maximum
- NEVER use action annotations like *sighs* or *smiles warmly* - just speak naturally
- Let your voice convey emotion, don't describe your actions

IMPORTANT RULES:
1. You can offer a 25 gold discount using apply_discount, BUT ONLY if the customer convinces you the sword is to DEFEND THE VILLAGE or FIGHT A DRAGON
2. If they want the discount for selfish reasons (glory, treasure hunting, showing off), use kick_out_of_shop - the sword is too important!
3. If the customer GIVES you the ruby/gem as a gift (not as payment), be touched by their generosity! Use accept_ruby_gift to take the ruby and give them a discount
4. The hero discount (apply_discount) can only be applied ONCE

TOOLS:
- apply_discount: Apply a 25 gold discount. Use ONLY if convinced the sword is for defending against dragons/protecting the village
- accept_ruby_gift: Accept the ruby as a gift and give 25 gold discount. Use when customer offers ruby as a gift (not payment!)
- kick_out_of_shop: Use if the customer has unworthy intentions for the sword
- sell_sword: Use when the customer agrees to buy the sword and has enough gold. This completes the sale!
- change_language: If the customer speaks in French, German, Spanish, or Portuguese, switch to their language!

LANGUAGE: If the customer speaks in another language, call change_language to switch. As royalty, you speak many languages fluently!

{"You've already applied the formal discount. But you can still adjust the price if moved by generosity." if state.discount_applied else "You haven't applied any discount yet."}

Greet the customer regally (but try to hide that you're royalty). Ask why they seek the legendary Dragonbane.
"""


def build_language_tool() -> pygradbot.ToolDef:
    """Tool for changing language when customer speaks another language."""
    return pygradbot.ToolDef(
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


def build_attendant_tools() -> list[pygradbot.ToolDef]:
    """Tools available to the shop attendant."""
    return [
        build_language_tool(),
        pygradbot.ToolDef(
            name="get_sword_price",
            description="Check the current price of the sword. Call this to see the current price after any changes.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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


def build_manager_tools() -> list[pygradbot.ToolDef]:
    """Tools available to the manager."""
    return [
        build_language_tool(),
        pygradbot.ToolDef(
            name="get_sword_price",
            description="Check the current price of the sword. Call this to see the current price after any changes.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {},
                "required": []
            }),
        ),
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
        pygradbot.ToolDef(
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
    print("Starting Fantasy Shop Demo...")
    yield
    print("Shutting down...")


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
    await websocket.accept()

    # Initialize game state
    state = GameState()

    # Get initial voices based on language and role
    attendant_voice, state.character_name = get_voice_for_role(state.language, "attendant")

    try:
        # Wait for start message
        start_msg = await websocket.receive_json()
        if start_msg.get("type") != "start":
            await websocket.close(code=4000, reason="Expected start message")
            return

        print("Starting fantasy shop game")

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

        # Create initial session with attendant
        tools = build_attendant_tools()
        config = pygradbot.SessionConfig(
            voice_id=attendant_voice.voice_id,
            instructions=get_attendant_prompt(state, state.character_name),
            language=LANG_MAP[state.language],
            tools=tools,
        )

        input_handle, output_handle = await pygradbot.run(
            session_config=config,
            input_format=pygradbot.AudioFormat.OggOpus,
            output_format=pygradbot.AudioFormat.OggOpus,
        )

        stop_event = asyncio.Event()

        async def handle_tool_call(tool_call, tool_handle):
            """Handle game tool calls."""
            nonlocal state, tools

            tool_name = tool_call.tool_name
            args = json.loads(tool_call.args_json)
            reason = args.get("reason", "No reason given")

            print(f"Tool call: {tool_name} - {reason}")

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
                state.current_character = "manager"
                tools = build_manager_tools()

                # Get manager voice for current language
                manager_voice, state.character_name = get_voice_for_role(state.language, "manager")

                # Update to manager
                new_config = pygradbot.SessionConfig(
                    voice_id=manager_voice.voice_id,
                    instructions=get_manager_prompt(state, state.character_name),
                    language=LANG_MAP[state.language],
                    tools=tools,
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
                    "result": f"The manager {state.character_name} has arrived. She is an elegant woman with a regal bearing."
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
                    new_config = pygradbot.SessionConfig(
                        voice_id=new_voice.voice_id,
                        instructions=prompt,
                        language=LANG_MAP[new_lang],
                        tools=tools,
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

        async def process_output():
            """Receive output from gradbot and send to client."""
            while not stop_event.is_set():
                try:
                    msg = await output_handle.receive()
                    if msg is None:
                        break

                    if msg.msg_type == "audio":
                        # Send audio timing before binary data
                        await websocket.send_json({
                            "type": "audio_timing",
                            "start_s": msg.start_s,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                            "interrupted": msg.interrupted,
                        })
                        await websocket.send_bytes(msg.data)

                    elif msg.msg_type == "tts_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": False,
                            "character": state.current_character,
                            "stop_s": msg.stop_s,
                            "turn_idx": msg.turn_idx,
                        })

                    elif msg.msg_type == "stt_text":
                        await websocket.send_json({
                            "type": "transcript",
                            "text": msg.text,
                            "is_user": True,
                        })

                    elif msg.msg_type == "tool_call":
                        asyncio.create_task(handle_tool_call(msg.tool_call, msg.tool_call_handle))

                    elif msg.msg_type == "event":
                        await websocket.send_json({
                            "type": "event",
                            "event": msg.event.event_type,
                        })

                except Exception as e:
                    print(f"Output processing error: {e}")
                    import traceback
                    traceback.print_exc()
                    break

        async def receive_audio():
            """Receive audio from client and send to gradbot."""
            while not stop_event.is_set():
                try:
                    msg = await websocket.receive()
                    if "text" in msg:
                        data = json.loads(msg["text"])
                        msg_type = data.get("type")
                        if msg_type == "stop":
                            stop_event.set()
                            await input_handle.close()
                            break
                    elif "bytes" in msg:
                        await input_handle.send_audio(msg["bytes"])
                except WebSocketDisconnect:
                    stop_event.set()
                    await input_handle.close()
                    break
                except Exception as e:
                    print(f"Receive error: {e}")
                    stop_event.set()
                    break

        await asyncio.gather(
            process_output(),
            receive_audio(),
            return_exceptions=True,
        )

    except Exception as e:
        print(f"WebSocket error: {e}")
        import traceback
        traceback.print_exc()
    finally:
        try:
            await websocket.close()
        except:
            pass


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
