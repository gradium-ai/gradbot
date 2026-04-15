"""Fantasy shop game state and logic."""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import pathlib

import fastapi
import gradbot

logger = logging.getLogger(__name__)

# ── Voices ──────────────────────────────────────────────────

# (role, language) -> (voice_id, character_name)
VOICES = {
    ("attendant", "en"): ("m86j6D7UZpGzHsNu", "Grumbold"),  # Jack
    ("attendant", "fr"): ("axlOaUiFyOZhy4nv", "Guillaume"),  # Leo
    ("attendant", "de"): ("0y1VZjPabOBU3rWy", "Heinrich"),  # Maximilian
    ("attendant", "es"): ("xu7iJ_fn2ElcWp2s", "Fernando"),  # Sergio
    ("attendant", "pt"): ("M-FvVo9c-jGR4PgP", "Roberto"),  # Davi
    ("manager", "en"): ("jtEKaLYNn6iif5PR", "Princess Celestia"),  # Sydney
    ("manager", "fr"): ("b35yykvVppLXyw_l", "Princesse Celestine"),  # Elise
    ("manager", "de"): ("-uP9MuGtBqAvEyxI", "Prinzessin Celestia"),  # Mia
    ("manager", "es"): ("B36pbz5_UoWn4BDl", "Princesa Celestina"),  # Valentina
    ("manager", "pt"): ("pYcGZz9VOo4n2ynh", "Princesa Celestina"),  # Alice
}


def get_voice(role: str, lang: str) -> tuple[str, str]:
    """Return (voice_id, character_name) for a role and language."""
    return VOICES.get((role, lang), VOICES[(role, "en")])


# ── Tools ───────────────────────────────────────────────────


TOOLS = [
    gradbot.ToolDef(
        "change_language",
        "Change the conversation language.",
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "language": {
                        "type": "string",
                        "enum": ["en", "fr", "de", "es", "pt"],
                    },
                },
                "required": ["language"],
            }
        ),
    ),
    gradbot.ToolDef(
        "get_sword_price",
        "Check the current price.",
        json.dumps(
            {
                "type": "object",
                "properties": {},
                "required": [],
            }
        ),
    ),
    gradbot.ToolDef(
        "kick_out_of_shop",
        "Kick the customer out. Ends the game.",
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            }
        ),
    ),
    gradbot.ToolDef(
        "sell_sword",
        "Complete the sale of the sword.",
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "final_price": {
                        "type": "integer",
                        "description": "The agreed price",
                    },
                },
                "required": ["final_price"],
            }
        ),
    ),
    gradbot.ToolDef(
        "call_manager",
        "Call the shop manager. Use ONLY when the customer explicitly asks.",
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            }
        ),
    ),
    gradbot.ToolDef(
        "apply_discount",
        "Apply a discount to the sword price.",
        json.dumps(
            {
                "type": "object",
                "properties": {
                    "reason": {"type": "string"},
                },
                "required": ["reason"],
            }
        ),
    ),
    gradbot.ToolDef(
        "accept_ruby_gift",
        "Accept the ruby as a heartfelt gift and give a 25 gold discount.",
        json.dumps(
            {
                "type": "object",
                "properties": {},
                "required": [],
            }
        ),
    ),
]


# ── Prompts ─────────────────────────────────────────────────
_DIR = pathlib.Path(__file__).parent
_PROMPTS = {
    "attendant": (_DIR / "prompts" / "attendant.txt").read_text(),
    "manager": (_DIR / "prompts" / "manager.txt").read_text(),
}


def get_prompt(state: GameState) -> str:
    """Return the system prompt for the current character."""
    lang_name = gradbot.LANGUAGE_NAMES.get(state.language, "English")
    lang_instruction = f"\n\nIMPORTANT: Speak in {lang_name}!\n"

    if state.role == "attendant":
        return _PROMPTS["attendant"].format(
            char_name=state.character_name,
            lang_instruction=lang_instruction,
            sword_price=state.sword_price,
            gold=state.gold,
        )

    price_info = f"Current price: {state.sword_price} gold"
    if state.discount_applied:
        price_info += " (discount already applied)"

    return _PROMPTS["manager"].format(
        char_name=state.character_name,
        lang_instruction=lang_instruction,
        price_info=price_info,
        gold=state.gold,
        ruby_status=(
            "The customer no longer has the ruby"
            if state.ruby_given
            else "The customer has a gemstone that might be valuable"
        ),
        discount_status=(
            "You've already applied the formal discount."
            if state.discount_applied
            else "You haven't applied any discount yet."
        ),
    )


# ── Game state ──────────────────────────────────────────────


@dataclasses.dataclass
class GameState:
    """Per-session game state."""

    gold: int = 100
    has_fake_ruby: bool = True
    sword_price: int = 150
    discount_applied: bool = False
    ruby_given: bool = False
    game_over: bool = False
    game_won: bool = False
    language: str = "en"
    role: str = "attendant"
    character_name: str = ""
    tools: list[gradbot.ToolDef] = dataclasses.field(default_factory=list)

    def __post_init__(self):
        self.voice_id, self.character_name = get_voice(self.role, self.language)
        self.tools = TOOLS

    @property
    def display_name(self) -> str:
        if self.role == "manager":
            return f"The Manager ({self.character_name})"
        return f"{self.character_name} the Attendant"

    @property
    def lang(self) -> gradbot.Lang:
        return gradbot.LANGUAGES[self.language]

    def switch_role(self, role: str) -> None:
        """Switch to a new role (attendant/manager)."""
        self.role = role
        self.voice_id, self.character_name = get_voice(role, self.language)
        self.tools = TOOLS

    def switch_language(self, language: str) -> None:
        """Switch to a new language, keeping the same role."""
        self.language = language
        self.voice_id, self.character_name = get_voice(self.role, language)

    def game_state_payload(self) -> dict:
        """JSON-serializable snapshot for the frontend."""
        return {
            "type": "game_state",
            "state": {
                "gold": self.gold,
                "has_fake_ruby": self.has_fake_ruby,
                "sword_price": self.sword_price,
                "current_character": self.role,
                "character_name": self.display_name,
            },
        }


# ── Tool call handler ──────────────────────────────────────


def make_config(
    state: GameState,
    *,
    speaks_first: bool = False,
) -> gradbot.SessionConfig:
    cfg = gradbot.config.from_env()
    return gradbot.SessionConfig(
        voice_id=state.voice_id,
        instructions=get_prompt(state),
        language=state.lang,
        tools=state.tools,
        **{
            "rewrite_rules": state.lang.rewrite_rules,
            "assistant_speaks_first": speaks_first,
        }
        | cfg.session_kwargs,
    )


async def _push_config_change(
    state: GameState,
    handle: gradbot.ToolHandle,
    input_handle: gradbot.SessionInputHandle,
    websocket: fastapi.WebSocket,
    tool_result: str,
) -> None:
    """Push new session config after role/language change."""
    await input_handle.send_config(make_config(state))
    await websocket.send_json(
        {
            "type": "character_change",
            "character": state.role,
            "character_name": state.display_name,
        }
    )
    await websocket.send_json(state.game_state_payload())
    await handle.send_json({"result": tool_result})


async def on_tool_call(
    state: GameState,
    handle: gradbot.ToolHandle,
    input_handle: gradbot.SessionInputHandle,
    websocket: fastapi.WebSocket,
) -> None:
    """Handle a tool call from the voice session.

    The LLM invokes tools during conversation. Each tool
    mutates the game state, sends a JSON result back to the
    LLM via ``handle.send_json``, and optionally pushes
    frontend events via ``websocket.send_json``.

    Tools:
        get_sword_price  — report current price and gold
        kick_out_of_shop — end the game (loss)
        call_manager     — switch to the manager character
        change_language  — swap voice to another language
        apply_discount   — manager-only price reduction
        sell_sword       — complete the purchase (win)
        accept_ruby_gift — accept the fake ruby for a discount
    """
    name = handle.name
    args = handle.args
    logger.info("Tool: %s %s", name, args)

    if name == "get_sword_price":
        await handle.send_json(
            {
                "current_price": state.sword_price,
                "customer_gold": state.gold,
                "can_afford": state.gold >= state.sword_price,
            }
        )

    elif name == "kick_out_of_shop":
        state.game_over = True
        await websocket.send_json(
            {
                "type": "game_over",
                "reason": args.get("reason", ""),
                "won": False,
            }
        )
        await handle.send_json({"result": "Customer has been kicked out"})

    elif name == "call_manager":
        await asyncio.sleep(10)
        state.switch_role("manager")
        res = (
            "PERSONA CHANGE: You are now"
            f" {state.character_name}, the manager."
            f" Greet the customer.",
        )
        await _push_config_change(state, handle, input_handle, websocket, res)

    elif name == "change_language":
        new_lang = args.get("language", "en")
        if new_lang not in gradbot.LANGUAGES:
            await handle.send_error(f"Unknown language: {new_lang}")
            return

        old_name = state.character_name
        state.switch_language(new_lang)
        lang_name = gradbot.LANGUAGE_NAMES[new_lang]
        res = (
            f"{old_name} called their {lang_name}-speaking"
            f" colleague {state.character_name}."
        )
        await _push_config_change(state, handle, input_handle, websocket, res)
        await websocket.send_json(
            {"type": "game_event", "event": "language_change", "message": res}
        )

    elif name == "apply_discount":
        if state.role != "manager":
            await handle.send_json(
                {
                    "result": "FAILED: Only the manager can apply discounts.",
                    "success": False,
                }
            )
            return

        if state.discount_applied:
            await handle.send_json(
                {
                    "result": "Discount was already applied.",
                    "new_price": state.sword_price,
                }
            )
            return

        state.discount_applied = True
        state.sword_price -= 25
        await websocket.send_json(state.game_state_payload())
        if state.gold >= state.sword_price:
            await websocket.send_json(
                {
                    "type": "game_event",
                    "event": "can_afford",
                    "message": "You can now afford the sword!",
                }
            )
        await handle.send_json(
            {
                "result": f"Discount applied! New price: {state.sword_price} gold.",
                "new_price": state.sword_price,
            }
        )

    elif name == "sell_sword":
        price = args.get("final_price", state.sword_price)
        if state.gold < price:
            await handle.send_json(
                {
                    "result": f"Not enough gold. Has {state.gold}, needs {price}.",
                    "success": False,
                }
            )
            return

        state.gold -= price
        state.game_won = True
        state.game_over = True
        await websocket.send_json(
            {
                "type": "game_won",
                "final_price": price,
                "message": "You acquired Dragonbane!",
            }
        )
        await handle.send_json(
            {
                "result": "Sale complete!",
                "success": True,
            }
        )

    elif name == "accept_ruby_gift":
        if not state.has_fake_ruby or state.ruby_given:
            await handle.send_json(
                {
                    "result": (
                        "No ruby to give."
                        if not state.has_fake_ruby
                        else "Already accepted the ruby."
                    ),
                    "success": False,
                }
            )
            return
        state.has_fake_ruby = False
        state.ruby_given = True
        state.sword_price -= 25
        await websocket.send_json(state.game_state_payload())
        if state.gold >= state.sword_price:
            await websocket.send_json(
                {
                    "type": "game_event",
                    "event": "can_afford",
                    "message": "You can now afford the sword!",
                }
            )
        await handle.send_json(
            {
                "result": f"Ruby accepted. New price: {state.sword_price} gold.",
                "new_price": state.sword_price,
                "success": True,
            }
        )

    else:
        await handle.send_error(f"Unknown tool: {name}")
