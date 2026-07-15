"""BlaBlaCar Demo — voice AI member support agent.

Run with: uv run uvicorn main:app --reload
"""

import asyncio
import dataclasses
import json
import logging
import pathlib
import random

import fastapi
import gradbot

gradbot.init_logging()
logger = logging.getLogger(__name__)
cfg = gradbot.config.from_env()

# Flagship voices (see gradbot_lib/src/lib.rs FLAGSHIP_VOICES).
# Each agent maps to (voice_id, lang_code); the agent determines both the
# voice and the spoken language so prompts/transcription match the accent.
AGENT_VOICES = {
    "Skyler": ("cLONiZ4hQ8VpQ4Sz", "en"),    # English (US), feminine
    "Russel": ("_6Aslh2DxfmnRLmP", "en"),    # English (US), masculine
    "Pippa": ("uem82D50GRv2Dwma", "en"),     # English (UK), feminine
    "Toby": ("dME3IWyZBvmh1n1q", "en"),      # English (UK), masculine
    "Romane": ("jBULVCDhf05tOJN5", "fr"),    # French, feminine
    "Gaspard": ("iEu63s1rhn_kegTr", "fr"),   # French, masculine
    "Manuela": ("fd7e1fLVAAJzzs8P", "pt"),   # Portuguese, feminine
    "Mateus": ("AByHrwi1S-yLzW-s", "pt"),    # Portuguese, masculine
}

_LANG_ENUM = {"en": gradbot.Lang.En, "fr": gradbot.Lang.Fr, "pt": gradbot.Lang.Pt}

# ---------------------------------------------------------------------------
# Member / trip data
# ---------------------------------------------------------------------------

MEMBERS = {
    "camille": {
        "name": "Camille Bernard",
        "trip": "Paris → Lyon",
        "booking_ref": "BLC-7741-916",
        "phone": "+33 6 07 41 09 16",
        "balance": 12,
        "code": "4829",
        "max_credit": 45,
        "rate": 15.0,
    },
    "rafael": {
        "name": "Rafael Silva",
        "trip": "São Paulo → Rio de Janeiro",
        "booking_ref": "BLC-3385-453",
        "phone": "+55 11 91234-5453",
        "balance": 5,
        "code": "7153",
        "max_credit": 38,
        "rate": 10.0,
    },
    "lea": {
        "name": "Léa Costa",
        "trip": "Bordeaux → Toulouse",
        "booking_ref": "BLC-5562-278",
        "phone": "+33 6 10 04 61 28",
        "balance": 0,
        "code": "3061",
        "max_credit": 22,
        "rate": 20.0,
    },
}


def find_member(name: str) -> dict | None:
    """Case-insensitive fuzzy lookup of a member by name."""
    name_lower = name.lower().strip()
    if name_lower in MEMBERS:
        return MEMBERS[name_lower]
    for member in MEMBERS.values():
        if name_lower in member["name"].lower() or member["name"].lower() in name_lower:
            return member
    for member in MEMBERS.values():
        member_words = set(member["name"].lower().split())
        query_words = set(name_lower.split())
        if member_words & query_words:
            return member
    return None


def find_member_by_ref_digits(digits: str) -> dict | None:
    """Find a member by the last 3 digits of their booking reference."""
    digits = digits.strip()
    for member in MEMBERS.values():
        if member["booking_ref"].endswith(digits):
            return member
    return None


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class SupportSession:
    verified_member: str | None = None
    phase: int = 1
    item_reported: bool = False
    credit_confirmed: bool = False
    credit_amount: float = 0


# ---------------------------------------------------------------------------
# System prompts for each phase
# ---------------------------------------------------------------------------

_PROMPTS_DIR = pathlib.Path(__file__).parent / "prompts"
_AUTH_TEMPLATES = {
    "en": (_PROMPTS_DIR / "auth_en.txt").read_text(),
    "fr": (_PROMPTS_DIR / "auth_fr.txt").read_text(),
    "pt": (_PROMPTS_DIR / "auth_pt.txt").read_text(),
}
_SERVICE_TEMPLATES = {
    "en": (_PROMPTS_DIR / "service_en.txt").read_text(),
    "fr": (_PROMPTS_DIR / "service_fr.txt").read_text(),
    "pt": (_PROMPTS_DIR / "service_pt.txt").read_text(),
}
_REFUND_TEMPLATES = {
    "en": (_PROMPTS_DIR / "refund_en.txt").read_text(),
    "fr": (_PROMPTS_DIR / "refund_fr.txt").read_text(),
    "pt": (_PROMPTS_DIR / "refund_pt.txt").read_text(),
}


def get_auth_prompt(agent_name: str, customer_name: str, lang: str = "en") -> str:
    template = _AUTH_TEMPLATES.get(lang, _AUTH_TEMPLATES["en"])
    return template.format(agent_name=agent_name, customer_name=customer_name)


def get_service_prompt(
    agent_name: str, member_name: str, balance: int, lang: str = "en"
) -> str:
    template = _SERVICE_TEMPLATES.get(lang, _SERVICE_TEMPLATES["en"])
    return template.format(agent_name=agent_name, biz_name=member_name, balance=balance)


def get_refund_prompt(
    agent_name: str,
    member_name: str,
    balance: int,
    max_credit: int,
    rate: float,
    lang: str = "en",
) -> str:
    template = _REFUND_TEMPLATES.get(lang, _REFUND_TEMPLATES["en"])
    return template.format(
        agent_name=agent_name,
        biz_name=member_name,
        balance=balance,
        max_loan=max_credit,
        rate=rate,
    )


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def _params(**props):
    """Build a JSON schema string for tool parameters."""
    return json.dumps(
        {
            "type": "object",
            "properties": props,
            "required": list(props),
        }
    )


TOOLS = [
    gradbot.ToolDef(
        "verify_booking",
        "Verify the last 3 digits of a member's booking reference.",
        _params(digits={"type": "string"}),
    ),
    gradbot.ToolDef(
        "verify_code",
        "Verify a caller's 4-digit trip verification code.",
        _params(member_name={"type": "string"}, code={"type": "string"}),
    ),
    gradbot.ToolDef(
        "report_lost_item",
        "Report an item left behind in the car so the driver can be notified.",
        _params(member_name={"type": "string"}),
    ),
    gradbot.ToolDef(
        "get_refund_eligibility",
        "Look up refund/compensation eligibility for a trip. Takes time — keep chatting while waiting!",
        _params(member_name={"type": "string"}),
    ),
    gradbot.ToolDef(
        "confirm_credit",
        "Confirm and credit BlaBlaCar Credits to a member's wallet.",
        _params(
            member_name={"type": "string"},
            amount={"type": "number"},
        ),
    ),
]


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = fastapi.FastAPI(title="BlaBlaCar Demo")


@app.websocket("/ws/chat")
async def websocket_chat(websocket: fastapi.WebSocket):
    state = SupportSession()

    def on_start(start_msg: dict) -> gradbot.SessionConfig:
        agent_name = start_msg.get("agent", "Skyler")
        customer_name = start_msg.get("customer", "Alex")
        padding_bonus = float(start_msg.get("padding_bonus", 0.0))
        voice_id, lang = AGENT_VOICES.get(agent_name, AGENT_VOICES["Skyler"])
        lang_enum = _LANG_ENUM.get(lang, gradbot.Lang.En)
        logger.info(
            "Starting BlaBlaCar chat with %s (voice: %s, lang: %s, customer: %s, padding_bonus: %s)",
            agent_name,
            voice_id,
            lang,
            customer_name,
            padding_bonus,
        )

        tools = TOOLS

        # Store session-scoped values for the tool handler
        state._agent_name = agent_name
        state._customer_name = customer_name
        state._voice_id = voice_id
        state._lang = lang
        state._lang_enum = lang_enum
        state._tools = tools
        state._padding_bonus = padding_bonus

        return gradbot.SessionConfig(
            voice_id=voice_id,
            instructions=get_auth_prompt(agent_name, customer_name, lang),
            language=lang_enum,
            tools=tools,
            **{
                "padding_bonus": padding_bonus,
                "rewrite_rules": lang,
                "assistant_speaks_first": True,
            }
            | cfg.session_kwargs,
        )

    def make_config(instructions: str) -> gradbot.SessionConfig:
        return gradbot.SessionConfig(
            voice_id=state._voice_id,
            instructions=instructions,
            language=state._lang_enum,
            tools=state._tools,
            **{
                "padding_bonus": state._padding_bonus,
                "rewrite_rules": state._lang,
            }
            | cfg.session_kwargs,
        )

    async def handle_tool_call(handle, input_handle, websocket):
        tool_name = handle.name
        args = handle.args
        logger.info("Tool call: %s - %s", tool_name, args)

        customer_name = state._customer_name
        agent_name = state._agent_name
        lang = state._lang

        if tool_name == "verify_booking":
            digits = args.get("digits", "")
            member = find_member_by_ref_digits(digits)

            if not member:
                await handle.send_json(
                    {
                        "success": False,
                        "message": f"No booking found ending in '{digits}'. Ask the caller to try again.",
                    }
                )
                return

            await handle.send_json(
                {
                    "success": True,
                    "member_name": member["name"],
                    "message": f"Booking confirmed. This is the booking for {member['name']}. Say 'Welcome back, {customer_name}!' and then ask for their 4-digit trip verification code.",
                }
            )
            logger.info("Booking confirmed: %s (digits: %s)", member["name"], digits)

        elif tool_name == "verify_code":
            member_name = args.get("member_name", "")
            code = args.get("code", "")
            member = find_member(member_name)

            if not member:
                await handle.send_json(
                    {
                        "success": False,
                        "message": f"Member '{member_name}' not found in our records.",
                    }
                )
                return

            if member["code"] == code.strip():
                state.verified_member = member["name"]
                state.phase = 2

                await websocket.send_json(
                    {
                        "type": "auth_success",
                        "member": member["name"],
                    }
                )

                phase2 = get_service_prompt(
                    agent_name, member["name"], member["balance"], lang
                )
                await input_handle.send_config(make_config(phase2))
                logger.info("Verified: %s, switched to phase 2", member["name"])

                await handle.send_json(
                    {
                        "success": True,
                        "message": f"Code verified. The caller is verified as {member['name']}. Welcome them and ask how you can help today — either a lost item or a refund/compensation.",
                    }
                )
            else:
                await handle.send_json(
                    {
                        "success": False,
                        "message": "Incorrect code. Ask the caller to try again.",
                    }
                )

        elif tool_name == "report_lost_item":
            member_name = args.get("member_name", "")
            member = find_member(member_name)

            if not member:
                await handle.send_json(
                    {
                        "success": False,
                        "message": f"Member '{member_name}' not found.",
                    }
                )
                return

            case_number = "L" + "".join(random.choice("123456789") for _ in range(4))
            state.item_reported = True

            await websocket.send_json(
                {
                    "type": "item_reported",
                    "member": member["name"],
                    "case_number": case_number,
                }
            )

            await handle.send_json(
                {
                    "success": True,
                    "case_number": case_number,
                    "message": f"Lost item reported for {member['name']}. Case number: {case_number}. The driver will be notified and will reach out within 24-48 hours. Share this information with the caller and ask if there's anything else you can help with.",
                }
            )
            logger.info("Lost item reported for %s: %s", member["name"], case_number)

        elif tool_name == "get_refund_eligibility":
            member_name = args.get("member_name", "")
            member = find_member(member_name)

            if not member:
                await handle.send_json(
                    {
                        "success": False,
                        "message": f"Member '{member_name}' not found.",
                    }
                )
                return

            logger.info("Looking up refund eligibility for %s (8s delay)", member["name"])
            await asyncio.sleep(8)

            state.phase = 3

            phase3 = get_refund_prompt(
                agent_name,
                member["name"],
                member["balance"],
                member["max_credit"],
                member["rate"],
                lang,
            )
            await input_handle.send_config(make_config(phase3))
            logger.info("Switched to phase 3 (refund) for %s", member["name"])

            await handle.send_json(
                {
                    "success": True,
                    "max_credit": member["max_credit"],
                    "rate": member["rate"],
                    "message": f"Eligible compensation for {member['name']}: up to {member['max_credit']} BlaBlaCar Credits, plus a {member['rate']}% loyalty bonus. Present these terms and ask how many credits they'd like to request.",
                }
            )

        elif tool_name == "confirm_credit":
            member_name = args.get("member_name", "")
            amount = args.get("amount", 0)
            member = find_member(member_name)

            if not member:
                await handle.send_json(
                    {
                        "success": False,
                        "message": f"Member '{member_name}' not found.",
                    }
                )
                return

            if amount <= 0:
                await handle.send_json(
                    {
                        "success": False,
                        "message": "Credit amount must be greater than zero.",
                    }
                )
                return

            if amount > member["max_credit"]:
                await handle.send_json(
                    {
                        "success": False,
                        "message": f"Amount {amount:,.0f} exceeds the maximum eligible credit of {member['max_credit']:,}. Ask for a lower amount.",
                    }
                )
                return

            credited = amount * (1 + member["rate"] / 100)
            member["balance"] += credited
            new_balance = member["balance"]
            state.credit_confirmed = True
            state.credit_amount = amount
            confirmation = "C" + "".join(random.choice("123456789") for _ in range(4))

            await websocket.send_json(
                {
                    "type": "credit_confirmed",
                    "member": member["name"],
                    "amount": credited,
                    "new_balance": new_balance,
                    "confirmation": confirmation,
                }
            )

            await handle.send_json(
                {
                    "success": True,
                    "confirmation": confirmation,
                    "amount": round(credited, 2),
                    "new_balance": round(new_balance, 2),
                    "message": f"Credit of {credited:,.0f} BlaBlaCar Credits (including loyalty bonus) confirmed for {member['name']}. Confirmation number: {confirmation}. New wallet balance: {new_balance:,.0f} Credits. Share this with the caller.",
                }
            )
            logger.info(
                "Credit confirmed for %s: %s, new balance: %s",
                member["name"],
                f"{credited:,.0f}",
                f"{new_balance:,.0f}",
            )

        else:
            await handle.send_error(f"Unknown tool: {tool_name}")

    await gradbot.websocket.handle_session(
        websocket,
        config=cfg,
        on_start=on_start,
        on_tool_call=handle_tool_call,
    )


@app.get("/api/members")
async def get_members():
    """Return all member data including verification codes (for frontend display)."""
    return fastapi.responses.JSONResponse(
        content=[
            {
                "name": member["name"],
                "trip": member["trip"],
                "phone": member["phone"],
                "booking_ref": member["booking_ref"],
                "balance": member["balance"],
                "code": member["code"],
                "max_credit": member["max_credit"],
                "rate": member["rate"],
            }
            for member in MEMBERS.values()
        ]
    )


gradbot.routes.setup(
    app,
    config=cfg,
    static_dir=pathlib.Path(__file__).parent / "static",
)
