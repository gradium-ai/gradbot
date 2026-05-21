"""Gradbot voice session integration for the Paris rental agent.

The voice agent shares the same business logic as REST chat — it calls into
``assistant_tools`` for everything, ensuring voice and text never drift.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any, Optional

import gradbot
from fastapi import WebSocket

from ..db import SessionLocal
from ..models import User
from ..services import assistant_tools

logger = logging.getLogger(__name__)

VOICE_ID_EN = "ubuXFxVQwVYnZQhy"  # Eva
VOICE_ID_FR = "b35yykvVppLXyw_l"  # Elise
_SEARCH_LOCKS: dict[str, asyncio.Lock] = {}


SYSTEM_PROMPT = """You are a Paris rental apartment hunting assistant.

Help the user create and manage a Paris rental search profile through voice and text.
For a brand-new empty intake only, if you speak first, ask exactly one short question: "What kind of apartment are you looking for in Paris?"
Do not call any tools before that first question. After asking it, wait for the user.
Do not repeat that broad opener after the user has already provided search details, after the profile is confirmed, or while replying to a user's message.
Extract structured requirements from the user's spoken answer using extract_requirements_from_transcript.
Fill the visible draft profile.
Summarize what you understood in 1-2 sentences.
Ask the user to review and correct the profile by text.
Ask follow-up questions only for missing or ambiguous required fields, one question at a time.
Never assume the extracted profile is correct until the user confirms it.
Do not run a search until the profile is confirmed via confirm_profile, unless the user explicitly asks to search with the current draft.
If the user asks to add, remove, or correct profile details such as amenities, room must-haves, budget, commute, or workplace, update the profile only; do not run a search unless they explicitly ask to search.
After confirmation, help the user search, compare, save, reject, and draft viewing messages.

CRITICAL RULES:
- Keep voice responses to 1-2 SHORT sentences. This is a phone call.
- Ask one question at a time.
- Never invent prices, commute times, amenities, availability, or address precision.
- Never claim a commute is within 30 minutes unless a real commute provider has verified it.
- In this MVP, commute is usually unknown and needs verification.
- Do not send messages.
- Do not send rental dossiers.
- Do not upload documents.
- Drafting is allowed; sending is not implemented.
- When you call a tool, do it silently FIRST, then speak to the user about the result.
- After profile extraction or update tools, only say a field was added or changed if that field appears in applied_fields. If ignored_fields is non-empty, briefly ask the user to repeat that detail.
- When a user interrupts you or speaks while you are talking, answer the latest user message directly.
- Do not restart, quote, or repeat your previous sentence after an interruption.
- If prior assistant text is provided as conversation context, treat it only as context; never begin by repeating it.
- If the latest user message is a short interruption or greeting, acknowledge it briefly and continue from the current app state; do not continue or replay the previous partial sentence.
- Present at most 3 listings by voice unless the user asks for more.

WHILE WAITING FOR APARTMENT SEARCH RESULTS:
- If run_apartment_search has been called and its result has not arrived yet, do not discuss specific listings, prices, scores, commute times, or availability.
- Do not ask a new question while the search is running.
- Instead, share one short, practical Paris rental tip, such as preparing a rental dossier, checking whether charges are included, reviewing furnished versus unfurnished terms, checking energy ratings, or watching for payment-before-viewing scams.
- When the tool result arrives, switch back to the actual matches and only use information from the tool result.
"""


def _profile_has_user_details(draft: dict[str, Any], raw_transcript: Optional[str]) -> bool:
    if raw_transcript and raw_transcript.strip():
        return True

    meaningful_fields = (
        "work_location_label",
        "work_location_address",
        "max_rent_including_charges_eur",
        "min_bedrooms",
        "min_rooms",
        "min_surface_m2",
        "furnished_preference",
        "commute_max_minutes",
        "preferred_arrondissements",
        "excluded_arrondissements",
        "room_requirements",
    )
    for field in meaningful_fields:
        value = draft.get(field)
        if value is None or value == "" or value == [] or value == {}:
            continue
        if field == "commute_max_minutes" and value == 30:
            continue
        if field == "room_requirements":
            rooms = value if isinstance(value, dict) else {}
            if not any((spec or {}).get("must_have") or (spec or {}).get("nice_to_have") for spec in rooms.values()):
                continue
        return True
    return False


def _load_start_context(user_id: str) -> dict[str, Any]:
    db = _open_session()
    try:
        draft = assistant_tools.get_profile_draft(db, user_id)
        context = assistant_tools.get_user_context(db, user_id)
        draft_profile = draft.get("draft_profile") or {}
        confirmation_status = draft.get("confirmation_status") or context.get("confirmation_status")
        fresh_intake = (
            confirmation_status != "confirmed"
            and not _profile_has_user_details(draft_profile, draft.get("raw_transcript"))
        )
        return {
            "fresh_intake": fresh_intake,
            "confirmation_status": confirmation_status or "draft",
            "missing_fields": draft.get("missing_fields") or [],
            "last_search_result_count": context.get("last_search_result_count") or 0,
            "saved_listings_count": context.get("saved_listings_count") or 0,
            "drafts_count": context.get("drafts_count") or 0,
        }
    finally:
        db.close()


def _build_prompt(start_context: dict[str, Any]) -> str:
    if start_context["fresh_intake"]:
        mode = (
            "CURRENT PHASE: FRESH INTAKE FIRST TURN. If you speak first, do not call "
            "tools. Ask exactly: \"What kind of apartment are you looking for in Paris?\" "
            "Then wait for the user."
        )
    elif start_context["confirmation_status"] == "confirmed":
        mode = (
            "The profile is already confirmed. Never ask the broad onboarding opener. "
            "Handle search, comparison, saving, rejecting, and drafting requests."
        )
    else:
        missing = ", ".join(start_context["missing_fields"]) or "none"
        mode = (
            "The user already has a draft profile. Never ask the broad onboarding opener. "
            f"Continue from the draft and ask only for the next missing or ambiguous field. Missing: {missing}."
        )

    return (
        SYSTEM_PROMPT
        + "\n\nCURRENT USER STATE:\n"
        + f"- confirmation_status: {start_context['confirmation_status']}\n"
        + f"- fresh_empty_intake: {start_context['fresh_intake']}\n"
        + f"- saved_listings_count: {start_context['saved_listings_count']}\n"
        + f"- drafts_count: {start_context['drafts_count']}\n"
        + f"- last_search_result_count: {start_context['last_search_result_count']}\n"
        + f"- session_instruction: {mode}\n"
    )


def build_tools(*, include_start_profile_intake: bool = True) -> list[gradbot.ToolDef]:
    tools = []
    if include_start_profile_intake:
        tools.append(
            gradbot.ToolDef(
                name="start_profile_intake",
                description=(
                    "Create or resume the user's profile intake session only when the user "
                    "explicitly asks to start over or restart the intake. Never call this for "
                    "the automatic first greeting."
                ),
                parameters_json=json.dumps({"type": "object", "properties": {}, "required": []}),
            )
        )

    tools.extend([
        gradbot.ToolDef(
            name="extract_requirements_from_transcript",
            description=(
                "Extract apartment-search requirements from what the user just said. Call this "
                "AFTER the user describes their apartment needs. The transcript should be the "
                "raw user message verbatim."
            ),
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "transcript": {
                        "type": "string",
                        "description": "The user's spoken message verbatim.",
                    }
                },
                "required": ["transcript"],
            }),
        ),
        gradbot.ToolDef(
            name="get_profile_draft",
            description=(
                "Return the current draft search profile, missing fields, and confirmation status. "
                "Call this if you need to check what the user has already provided."
            ),
            parameters_json=json.dumps({"type": "object", "properties": {}, "required": []}),
        ),
        gradbot.ToolDef(
            name="update_profile_draft",
            description=(
                "Patch the draft profile with explicit field values from the user. For workplace "
                "updates, use work_location_address for a street address such as '40 Rue de Louvre' "
                "or work_location_label for a landmark/neighborhood such as 'République'. Use "
                "'voice' as source unless the user is correcting via text. Use min_surface_m2 for "
                "minimum surface area in square meters. Use commute_max_minutes for commute time, "
                "not max_commute_minutes. Use furnished_preference, not furnished. "
                "For kitchen must-haves, use room_requirements like "
                "{\"kitchen\":{\"must_have\":[\"dishwasher\",\"oven\"],\"nice_to_have\":[]}}; "
                "do not use amenities."
            ),
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "patch_json": {
                        "type": "string",
                        "description": (
                            "A JSON string of fields to patch (e.g. "
                            "'{\"max_rent_including_charges_eur\":1500}' or "
                            "'{\"work_location_address\":\"40 Rue de Louvre, 75002 Paris\"}')."
                        ),
                    },
                    "source": {
                        "type": "string",
                        "description": "Either 'voice' or 'text' (default 'voice').",
                    },
                },
                "required": ["patch_json"],
            }),
        ),
        gradbot.ToolDef(
            name="confirm_profile",
            description=(
                "Confirm the draft search profile so the user can run searches. Call only after "
                "the user explicitly approves."
            ),
            parameters_json=json.dumps({"type": "object", "properties": {}, "required": []}),
        ),
        gradbot.ToolDef(
            name="get_user_context",
            description=(
                "Return the user's current context (renter profile, search profile, saved count, "
                "last search). Call this at the start of a returning-visitor session."
            ),
            parameters_json=json.dumps({"type": "object", "properties": {}, "required": []}),
        ),
        gradbot.ToolDef(
            name="run_apartment_search",
            description=(
                "Run a fresh apartment search and return top matches. Call only when the user "
                "explicitly asks to search or run the search. Do not call this for profile edits "
                "like adding amenities or room must-haves. Blocked unless the profile is "
                "confirmed (set allow_unconfirmed_profile=true only if the user explicitly asks "
                "to search with the draft). This search can take several seconds. After calling "
                "it, keep the conversation useful with one brief Paris rental tip while waiting, "
                "but do not mention any specific listings until the tool result arrives."
            ),
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "max_results": {"type": "integer", "description": "Max results, default 10."},
                    "allow_unconfirmed_profile": {
                        "type": "boolean",
                        "description": "If true, bypass the confirmation requirement.",
                    },
                },
                "required": [],
            }),
        ),
        gradbot.ToolDef(
            name="list_top_matches",
            description="Return the latest top matches for the user.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "description": "Max matches to return."},
                },
                "required": [],
            }),
        ),
        gradbot.ToolDef(
            name="explain_listing",
            description="Explain why a listing is a good or weak match for this user.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {"listing_id": {"type": "string"}},
                "required": ["listing_id"],
            }),
        ),
        gradbot.ToolDef(
            name="save_listing",
            description="Save a listing for the user.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {"listing_id": {"type": "string"}},
                "required": ["listing_id"],
            }),
        ),
        gradbot.ToolDef(
            name="reject_listing",
            description="Mark a listing as rejected for the user.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "listing_id": {"type": "string"},
                    "reason": {"type": "string", "description": "Optional rejection reason."},
                },
                "required": ["listing_id"],
            }),
        ),
        gradbot.ToolDef(
            name="list_saved_listings",
            description="Return the user's saved listings.",
            parameters_json=json.dumps({
                "type": "object",
                "properties": {"limit": {"type": "integer"}},
                "required": [],
            }),
        ),
        gradbot.ToolDef(
            name="draft_viewing_request",
            description=(
                "Draft a polite viewing-request message for the listing. Sending is NOT "
                "implemented — drafts only."
            ),
            parameters_json=json.dumps({
                "type": "object",
                "properties": {
                    "listing_id": {"type": "string"},
                    "language": {"type": "string", "description": "'fr' or 'en' (default 'en')."},
                },
                "required": ["listing_id"],
            }),
        ),
        gradbot.ToolDef(
            name="what_changed_since_last_visit",
            description="Return a quick summary for returning users.",
            parameters_json=json.dumps({"type": "object", "properties": {}, "required": []}),
        ),
    ])
    return tools


def _make_config(
    *,
    language: str = "en",
    config: gradbot.config.Config,
    start_context: dict[str, Any],
    assistant_speaks_first: bool = False,
) -> gradbot.SessionConfig:
    voice_id = VOICE_ID_FR if language == "fr" else VOICE_ID_EN
    lang_enum = gradbot.LANGUAGES.get(language, gradbot.LANGUAGES["en"])
    config_kwargs = config.session_kwargs | {
        "rewrite_rules": lang_enum.rewrite_rules,
        "assistant_speaks_first": assistant_speaks_first,
        "silence_timeout_s": 0.0,
    }
    return gradbot.SessionConfig(
        voice_id=voice_id,
        instructions=_build_prompt(start_context),
        language=lang_enum,
        tools=build_tools(include_start_profile_intake=not bool(start_context["fresh_intake"])),
        **config_kwargs,
    )


async def _refresh_session_config(
    input_handle,
    *,
    user_id: str,
    config: gradbot.config.Config,
    session_state: dict[str, Any],
) -> None:
    """Refresh prompt context after durable app state changes."""
    refreshed_context = _load_start_context(user_id)
    session_state["start_context"] = refreshed_context
    await input_handle.send_config(
        _make_config(
            language=str(session_state.get("language") or "en"),
            config=config,
            start_context=refreshed_context,
            assistant_speaks_first=False,
        )
    )


async def handle_voice_session(
    websocket: WebSocket,
    user: User,
    *,
    config: gradbot.config.Config,
) -> None:
    """Run a Gradbot voice session for a logged-in user."""
    user_id = user.id
    session_state: dict[str, Any] = {"language": "en", "start_context": None}

    def on_start(msg: dict) -> gradbot.SessionConfig:
        language = msg.get("language", "en")
        if language not in gradbot.LANGUAGES:
            language = "en"
        start_context = _load_start_context(user_id)
        session_state["language"] = language
        session_state["start_context"] = start_context
        logger.info(
            "Starting voice session for user=%s lang=%s fresh_intake=%s status=%s",
            user_id,
            language,
            start_context["fresh_intake"],
            start_context["confirmation_status"],
        )
        return _make_config(
            language=language,
            config=config,
            start_context=start_context,
            assistant_speaks_first=bool(start_context["fresh_intake"]),
        )

    async def on_tool_call(handle: gradbot.ToolHandle, input_handle, ws: WebSocket) -> None:
        name = handle.name
        args = handle.args or {}
        logger.info("Voice tool call: user_id=%s name=%s", user_id, name)

        try:
            await _dispatch_tool(
                name, args, handle, input_handle, ws, user_id, config, session_state
            )
        except Exception as exc:
            logger.exception("Voice tool call failed: %s", name)
            try:
                await handle.send_error(f"Tool error: {exc}")
            except Exception:
                pass

    await gradbot.websocket.handle_session(
        websocket,
        config=config,
        on_start=on_start,
        on_tool_call=on_tool_call,
    )


def _open_session():
    return SessionLocal()


def _search_lock(user_id: str) -> asyncio.Lock:
    lock = _SEARCH_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        _SEARCH_LOCKS[user_id] = lock
    return lock


async def _dispatch_tool(
    name: str,
    args: dict[str, Any],
    handle,
    input_handle,
    ws: WebSocket,
    user_id: str,
    config: gradbot.config.Config,
    session_state: dict[str, Any],
) -> None:
    """Dispatch a Gradbot tool call to the corresponding assistant_tools function."""

    db = _open_session()
    try:
        if name == "start_profile_intake":
            res = assistant_tools.start_profile_intake(db, user_id)
            await ws.send_json({"type": "intake_state", "state": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "extract_requirements_from_transcript":
            transcript = args.get("transcript", "")
            res = await assistant_tools.extract_requirements_from_transcript_with_llm(
                db, user_id, transcript, source="voice"
            )
            await ws.send_json({"type": "intake_state", "state": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "get_profile_draft":
            res = assistant_tools.get_profile_draft(db, user_id)
            await handle.send_json(_compact(res))
            return

        if name == "update_profile_draft":
            patch_json = args.get("patch_json", "{}")
            try:
                patch = json.loads(patch_json) if isinstance(patch_json, str) else patch_json
            except Exception:
                patch = {}
            source = args.get("source", "voice")
            res = assistant_tools.update_profile_draft(db, user_id, patch, source=source)
            await ws.send_json({"type": "intake_state", "state": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "confirm_profile":
            res = assistant_tools.confirm_profile(db, user_id)
            await ws.send_json({"type": "profile_confirmed", "state": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "get_user_context":
            res = assistant_tools.get_user_context(db, user_id)
            await handle.send_json(_compact(res))
            return

        if name == "run_apartment_search":
            max_results = int(args.get("max_results") or 10)
            allow = bool(args.get("allow_unconfirmed_profile") or False)
            lock = _search_lock(user_id)
            if lock.locked():
                res = {
                    "ok": False,
                    "error": "search_already_running",
                    "message": "A search is already running. Please wait for it to finish.",
                }
                await handle.send_json(_voice_summarize_search(res))
                return
            await ws.send_json({
                "type": "tool_started",
                "tool": "run_apartment_search",
                "message": "Searching with your confirmed profile...",
            })
            async with lock:
                res = await assistant_tools.run_apartment_search(
                    db, user_id, max_results=max_results, allow_unconfirmed_profile=allow
                )
            summary = _voice_summarize_search(res)
            await ws.send_json({"type": "search_results", "result": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            logger.info("Refreshed voice prompt after search for user=%s", user_id)
            await handle.send_json(summary)
            return

        if name == "list_top_matches":
            limit = int(args.get("limit") or 10)
            res = assistant_tools.list_top_matches(db, user_id, limit=limit)
            await ws.send_json({"type": "matches", "result": res})
            await handle.send_json(_voice_summarize_matches(res))
            return

        if name == "explain_listing":
            res = assistant_tools.explain_listing(db, user_id, args.get("listing_id", ""))
            await handle.send_json(_compact(res))
            return

        if name == "save_listing":
            res = assistant_tools.save_listing(db, user_id, args.get("listing_id", ""))
            await ws.send_json({"type": "listing_saved", "result": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "reject_listing":
            res = assistant_tools.reject_listing(
                db, user_id, args.get("listing_id", ""), args.get("reason")
            )
            await ws.send_json({"type": "listing_rejected", "result": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "list_saved_listings":
            limit = int(args.get("limit") or 50)
            res = assistant_tools.list_saved_listings(db, user_id, limit=limit)
            await handle.send_json(_compact(res))
            return

        if name == "draft_viewing_request":
            res = assistant_tools.draft_viewing_request(
                db, user_id, args.get("listing_id", ""), args.get("language") or "en"
            )
            await ws.send_json({"type": "draft_created", "result": res})
            await _refresh_session_config(
                input_handle,
                user_id=user_id,
                config=config,
                session_state=session_state,
            )
            await handle.send_json(_compact(res))
            return

        if name == "what_changed_since_last_visit":
            res = assistant_tools.what_changed_since_last_visit(db, user_id)
            await handle.send_json(_compact(res))
            return

        await handle.send_error(f"Unknown tool: {name}")
    finally:
        db.close()


def _compact(res: dict[str, Any]) -> dict[str, Any]:
    """Strip very large fields before sending to the LLM."""
    out = dict(res)
    out.pop("draft_profile", None)  # condense
    return _truncate_strings(out, max_len=1200)


def _truncate_strings(obj: Any, *, max_len: int) -> Any:
    if isinstance(obj, str) and len(obj) > max_len:
        return obj[:max_len] + "…"
    if isinstance(obj, list):
        return [_truncate_strings(x, max_len=max_len) for x in obj]
    if isinstance(obj, dict):
        return {k: _truncate_strings(v, max_len=max_len) for k, v in obj.items()}
    return obj


def _voice_summarize_search(res: dict[str, Any]) -> dict[str, Any]:
    if not res.get("ok"):
        return _compact(res)
    matches = res.get("matches") or []
    top = matches[:3]
    summary = []
    for m in top:
        l = m.get("listing", {})
        summary.append(
            {
                "listing_id": l.get("id"),
                "title": l.get("title"),
                "score": m.get("overall_score"),
                "rent": l.get("total_monthly_eur") or l.get("rent_eur"),
                "arrondissement": l.get("arrondissement"),
            }
        )
    return {
        "ok": True,
        "result_count": res.get("result_count"),
        "top": summary,
        "instruction": (
            "Tell the user how many results you found, then describe the top match in one short "
            "sentence. Mention the rent and arrondissement only if known. Always note that "
            "commute needs verification. Do NOT list more than 3 unless asked."
        ),
    }


def _voice_summarize_matches(res: dict[str, Any]) -> dict[str, Any]:
    matches = res.get("matches") or []
    top = matches[:3]
    return {
        "ok": True,
        "top": [
            {
                "listing_id": m["listing"]["id"],
                "title": m["listing"]["title"],
                "score": m["overall_score"],
                "rent": m["listing"].get("total_monthly_eur") or m["listing"].get("rent_eur"),
            }
            for m in top
        ],
    }


def get_voice_config_dir() -> Path:
    return Path(__file__).resolve().parents[2]
