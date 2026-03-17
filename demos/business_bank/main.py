"""
Business Bank Demo - Voice AI banking agent with PIN authentication

A voice agent that handles PIN authentication, lost card replacement,
and business loan services for small businesses.

Run with: uvicorn main:app --reload --port 8001
"""

import asyncio
import json
import logging
import os
import random
import sys
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse

import gradbot
from gradbot.fastapi import websocket_chat_handler, setup_demo_routes

gradbot.init_logging()
logger = logging.getLogger(__name__)

USE_PCM = os.environ.get("USE_PCM") == "1"
DEBUG = os.environ.get("DEBUG") == "1"
FLUSH_FOR_S = float(os.environ.get("FLUSH_FOR_S", "0.5"))

sys.path.insert(0, str(Path(__file__).parent.parent))
from demo_config import load_config, session_config_overrides, merge_overrides, client_config

_YAML_CFG = load_config(Path(__file__).parent)
_OVERRIDES = session_config_overrides(_YAML_CFG)
_CLIENT_CONFIG = client_config(_YAML_CFG)

AGENT_VOICES = {
    "Alex": ("Eva", "en"),
    "Jack": ("Jack", "en"),
    "Leo": ("Leo", "fr"),
}

# ---------------------------------------------------------------------------
# Business data
# ---------------------------------------------------------------------------

BUSINESSES = {
    "riverside_cafe": {
        "name": "Riverside Cafe",
        "account": "7741-0482-916",
        "phone": "(415) 555-0137",
        "balance": 12500,
        "pin": "4829",
        "max_loan": 50000,
        "rate": 5.5,
    },
    "summit_tech": {
        "name": "Summit Tech Solutions",
        "account": "3385-1927-453",
        "phone": "(628) 555-0294",
        "balance": 87200,
        "pin": "7153",
        "max_loan": 200000,
        "rate": 4.2,
    },
    "green_gardens": {
        "name": "Green Gardens Landscaping",
        "account": "5562-4810-278",
        "phone": "(510) 555-0461",
        "balance": 34800,
        "pin": "3061",
        "max_loan": 75000,
        "rate": 6.1,
    },
}


def find_business(name: str) -> dict | None:
    """Case-insensitive fuzzy lookup of a business by name."""
    name_lower = name.lower().strip()
    # Exact key match
    if name_lower in BUSINESSES:
        return BUSINESSES[name_lower]
    # Match on business name (substring / fuzzy)
    for biz in BUSINESSES.values():
        if name_lower in biz["name"].lower() or biz["name"].lower() in name_lower:
            return biz
    # Try matching individual words
    for biz in BUSINESSES.values():
        biz_words = set(biz["name"].lower().split())
        query_words = set(name_lower.split())
        if biz_words & query_words:
            return biz
    return None


def find_business_by_account_digits(digits: str) -> dict | None:
    """Find a business by the last 3 digits of their account number."""
    digits = digits.strip()
    for biz in BUSINESSES.values():
        if biz["account"].endswith(digits):
            return biz
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class BankSession:
    authenticated_business: str | None = None
    phase: int = 1
    card_ordered: bool = False
    loan_confirmed: bool = False
    loan_amount: float = 0


# ---------------------------------------------------------------------------
# System prompts for each phase
# ---------------------------------------------------------------------------

_PROMPTS_DIR = Path(__file__).parent / "prompts"
_AUTH_EN_TEMPLATE = (_PROMPTS_DIR / "auth_en.txt").read_text()
_AUTH_FR_TEMPLATE = (_PROMPTS_DIR / "auth_fr.txt").read_text()
_SERVICE_EN_TEMPLATE = (_PROMPTS_DIR / "service_en.txt").read_text()
_SERVICE_FR_TEMPLATE = (_PROMPTS_DIR / "service_fr.txt").read_text()
_LOAN_EN_TEMPLATE = (_PROMPTS_DIR / "loan_en.txt").read_text()
_LOAN_FR_TEMPLATE = (_PROMPTS_DIR / "loan_fr.txt").read_text()

_BUSINESS_NAMES = ", ".join(biz["name"] for biz in BUSINESSES.values())


def get_auth_prompt(agent_name: str, customer_name: str, lang: str = "en") -> str:
    template = _AUTH_FR_TEMPLATE if lang == "fr" else _AUTH_EN_TEMPLATE
    return template.format(agent_name=agent_name, customer_name=customer_name)


def get_service_prompt(agent_name: str, biz_name: str, balance: int, lang: str = "en") -> str:
    template = _SERVICE_FR_TEMPLATE if lang == "fr" else _SERVICE_EN_TEMPLATE
    return template.format(
        agent_name=agent_name,
        biz_name=biz_name,
        balance=f"{balance:,}",
    )


def get_loan_prompt(agent_name: str, biz_name: str, balance: int, max_loan: int, rate: float, lang: str = "en") -> str:
    template = _LOAN_FR_TEMPLATE if lang == "fr" else _LOAN_EN_TEMPLATE
    return template.format(
        agent_name=agent_name,
        biz_name=biz_name,
        balance=f"{balance:,}",
        max_loan=f"{max_loan:,}",
        rate=rate,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

def build_tools() -> list[gradbot.ToolDef]:
    return [
        gradbot.ToolDef(
            name="check_account",
            description="Verify the last 3 digits of a business account number. Call this as soon as the caller provides their digits.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "digits": {
                        "type": "string",
                        "description": "The last 3 digits of the account number"
                    }
                },
                "required": ["digits"]
            }),
        ),
        gradbot.ToolDef(
            name="check_pin",
            description="Verify a business caller's 4-digit PIN after their account has been confirmed. Returns success or failure.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the business (from check_account result)"
                    },
                    "pin": {
                        "type": "string",
                        "description": "The 4-digit PIN provided by the caller"
                    }
                },
                "required": ["business_name", "pin"]
            }),
        ),
        gradbot.ToolDef(
            name="order_replacement_card",
            description="Order a replacement debit card for an authenticated business.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the authenticated business"
                    }
                },
                "required": ["business_name"]
            }),
        ),
        gradbot.ToolDef(
            name="get_rate",
            description="Look up pre-approved business loan terms (maximum amount and interest rate) for an authenticated business. This takes some time to query the system — keep chatting with the caller while waiting!",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the authenticated business"
                    }
                },
                "required": ["business_name"]
            }),
        ),
        gradbot.ToolDef(
            name="confirm_loan",
            description="Confirm and disburse a business loan. The amount must not exceed the pre-approved maximum.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "business_name": {
                        "type": "string",
                        "description": "The name of the authenticated business"
                    },
                    "amount": {
                        "type": "number",
                        "description": "The loan amount requested"
                    }
                },
                "required": ["business_name", "amount"]
            }),
        ),
    ]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="Business Bank Demo")


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket):
    state = BankSession()

    def on_start(start_msg: dict) -> gradbot.SessionConfig:
        agent_name = start_msg.get("agent", "Alex")
        customer_name = start_msg.get("customer", "Jamie")
        padding_bonus = float(start_msg.get("padding_bonus", 0.0))
        voice_key, lang = AGENT_VOICES.get(agent_name, ("Eva", "en"))
        lang_enum = {"en": gradbot.Lang.En, "fr": gradbot.Lang.Fr}.get(lang, gradbot.Lang.En)
        logger.info("Starting business bank chat with %s (voice: %s, lang: %s, customer: %s, padding_bonus: %s)",
                     agent_name, voice_key, lang, customer_name, padding_bonus)

        voice = gradbot.flagship_voice(voice_key)
        tools = build_tools()

        # Store session-scoped values for the tool handler
        state._agent_name = agent_name
        state._customer_name = customer_name
        state._voice = voice
        state._lang = lang
        state._lang_enum = lang_enum
        state._tools = tools
        state._padding_bonus = padding_bonus

        return gradbot.SessionConfig(
            voice_id=voice.voice_id,
            instructions=get_auth_prompt(agent_name, customer_name, lang),
            language=lang_enum,
            tools=tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=padding_bonus,
                rewrite_rules=lang,
                assistant_speaks_first=True,
            ),
        )

    def make_config(instructions: str) -> gradbot.SessionConfig:
        return gradbot.SessionConfig(
            voice_id=state._voice.voice_id,
            instructions=instructions,
            language=state._lang_enum,
            tools=state._tools,
            **merge_overrides(_OVERRIDES,
                flush_duration_s=FLUSH_FOR_S,
                padding_bonus=state._padding_bonus,
                rewrite_rules=state._lang,
            ),
        )

    async def handle_tool_call(tool_call, tool_handle, input_handle, websocket):
        tool_name = tool_call.tool_name
        args = json.loads(tool_call.args_json)
        logger.info("Tool call: %s - %s", tool_name, args)

        customer_name = state._customer_name
        agent_name = state._agent_name
        lang = state._lang

        if tool_name == "check_account":
            digits = args.get("digits", "")
            biz = find_business_by_account_digits(digits)

            if not biz:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": f"No account found ending in '{digits}'. Ask the caller to try again.",
                }))
                return

            await tool_handle.send(json.dumps({
                "success": True,
                "business_name": biz["name"],
                "message": f"Account confirmed. This is the account for {biz['name']}. Say 'Welcome back, {customer_name}!' and then ask for their 4-digit PIN.",
            }))
            logger.info("Account confirmed: %s (digits: %s)", biz["name"], digits)

        elif tool_name == "check_pin":
            biz_name = args.get("business_name", "")
            pin = args.get("pin", "")
            biz = find_business(biz_name)

            if not biz:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": f"Business '{biz_name}' not found in our records.",
                }))
                return

            if biz["pin"] == pin.strip():
                state.authenticated_business = biz["name"]
                state.phase = 2

                # Notify frontend
                await websocket.send_json({
                    "type": "auth_success",
                    "business": biz["name"],
                })

                # Swap to phase 2 prompt
                phase2 = get_service_prompt(agent_name, biz["name"], biz["balance"], lang)
                await input_handle.send_config(make_config(phase2))
                logger.info("Authenticated: %s, switched to phase 2", biz["name"])

                await tool_handle.send(json.dumps({
                    "success": True,
                    "message": f"PIN verified. The caller is authenticated as {biz['name']}. Welcome them and ask how you can help today — either lost card replacement or a business loan.",
                }))
            else:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": "Incorrect PIN. Ask the caller to try again.",
                }))

        elif tool_name == "order_replacement_card":
            biz_name = args.get("business_name", "")
            biz = find_business(biz_name)

            if not biz:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": f"Business '{biz_name}' not found.",
                }))
                return

            tracking = "T" + "".join(random.choice("123456789") for _ in range(4))
            state.card_ordered = True

            # Notify frontend
            await websocket.send_json({
                "type": "card_ordered",
                "business": biz["name"],
                "tracking_number": tracking,
            })

            await tool_handle.send(json.dumps({
                "success": True,
                "tracking_number": tracking,
                "message": f"Replacement card ordered for {biz['name']}. Tracking number: {tracking}. The card will arrive in 3-5 business days. Share this information with the caller and ask if there's anything else you can help with.",
            }))
            logger.info("Card ordered for %s: %s", biz["name"], tracking)

        elif tool_name == "get_rate":
            biz_name = args.get("business_name", "")
            biz = find_business(biz_name)

            if not biz:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": f"Business '{biz_name}' not found.",
                }))
                return

            logger.info("Looking up loan terms for %s (8s delay)", biz["name"])
            await asyncio.sleep(8)

            state.phase = 3

            # Swap to phase 3 prompt
            phase3 = get_loan_prompt(agent_name, biz["name"], biz["balance"], biz["max_loan"], biz["rate"], lang)
            await input_handle.send_config(make_config(phase3))
            logger.info("Switched to phase 3 (loan) for %s", biz["name"])

            await tool_handle.send(json.dumps({
                "success": True,
                "max_loan": biz["max_loan"],
                "rate": biz["rate"],
                "message": f"Pre-approved loan terms for {biz['name']}: up to ${biz['max_loan']:,} at {biz['rate']}% APR. Present these terms and ask how much they'd like to borrow.",
            }))

        elif tool_name == "confirm_loan":
            biz_name = args.get("business_name", "")
            amount = args.get("amount", 0)
            biz = find_business(biz_name)

            if not biz:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": f"Business '{biz_name}' not found.",
                }))
                return

            if amount <= 0:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": "Loan amount must be greater than zero.",
                }))
                return

            if amount > biz["max_loan"]:
                await tool_handle.send(json.dumps({
                    "success": False,
                    "message": f"Amount ${amount:,.0f} exceeds the maximum pre-approved loan of ${biz['max_loan']:,}. Ask for a lower amount.",
                }))
                return

            # Update balance
            biz["balance"] += int(amount)
            new_balance = biz["balance"]
            state.loan_confirmed = True
            state.loan_amount = amount
            confirmation = "L" + "".join(random.choice("123456789") for _ in range(4))

            # Notify frontend
            await websocket.send_json({
                "type": "loan_confirmed",
                "business": biz["name"],
                "amount": amount,
                "new_balance": new_balance,
                "confirmation": confirmation,
            })

            await tool_handle.send(json.dumps({
                "success": True,
                "confirmation": confirmation,
                "amount": amount,
                "new_balance": new_balance,
                "message": f"Loan of ${amount:,.0f} confirmed for {biz['name']}. Confirmation number: {confirmation}. New account balance: ${new_balance:,}. Share this with the caller.",
            }))
            logger.info("Loan confirmed for %s: $%s, new balance: $%s", biz["name"], f"{amount:,.0f}", f"{new_balance:,}")

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


@app.get("/api/businesses")
async def get_businesses():
    """Return all business data including PINs (for frontend display)."""
    return JSONResponse(content=[
        {
            "name": biz["name"],
            "account": biz["account"],
            "balance": biz["balance"],
            "pin": biz["pin"],
            "max_loan": biz["max_loan"],
            "rate": biz["rate"],
        }
        for biz in BUSINESSES.values()
    ])


setup_demo_routes(app, static_dir=Path(__file__).parent / "static", use_pcm=USE_PCM)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8001)
