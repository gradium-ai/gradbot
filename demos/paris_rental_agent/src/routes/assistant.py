"""Text assistant route.

For the MVP this is a deterministic command-aware assistant — it parses simple
intents from the user's message and calls the same business-logic functions
used by the voice agent. This keeps voice and chat in lockstep.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..db import get_db
from ..dependencies import get_current_user
from ..models import ConversationMessage, ConversationSession, User
from ..schemas import ChatIn, ChatOut
from ..services import assistant_tools
from ..services.cities import city_label

router = APIRouter(prefix="/api/assistant", tags=["assistant"])


def _greet_or_status(db: Session, user_id: str) -> str:
    ctx = assistant_tools.get_user_context(db, user_id)
    sp = ctx.get("search_profile") or {}
    city = city_label(sp.get("city"))
    if ctx.get("confirmation_status") == "confirmed":
        last_run_at = ctx.get("last_search_at")
        last_run_count = ctx.get("last_search_result_count") or 0
        bits = [f"Your {city} search profile is confirmed."]
        if last_run_at:
            bits.append(f"Latest search returned {last_run_count} matches.")
        else:
            bits.append("You haven't run a search yet — say 'run search'.")
        if ctx.get("saved_listings_count"):
            bits.append(f"You have {ctx['saved_listings_count']} saved listing(s).")
        return " ".join(bits)
    missing = ctx.get("missing_fields") or []
    if missing:
        return (
            "We still need a few details before I can run a search. Missing: "
            + ", ".join(missing)
            + ". Try the voice onboarding, or update fields directly in the form."
        )
    return "Your draft profile looks complete. Say 'confirm profile' to lock it in."


def _intent(message: str) -> str:
    m = message.strip().lower()
    if not m:
        return "noop"
    edit_words = ("update", "edit", "change", "correct", "revise", "review", "adjust", "modify", "fix")
    profile_words = (
        "profile",
        "criteria",
        "preference",
        "preferences",
        "requirement",
        "requirements",
        "budget",
        "rent",
        "commute",
        "workplace",
        "office",
        "bedroom",
        "surface",
        "furnished",
    )
    if any(k in m for k in edit_words) and any(k in m for k in profile_words):
        return "edit_profile"
    if any(k in m for k in ("open", "show", "go back", "take me")) and "profile" in m:
        return "edit_profile"
    if any(k in m for k in ("hi", "hello", "hey", "status", "what's up")):
        return "greet"
    if "confirm" in m and "profile" in m:
        return "confirm"
    if "run" in m and ("search" in m or "find" in m):
        return "search"
    dwelling_words = ("apartment", "apartments", "flat", "flats", "home", "homes", "listing", "listings")
    if (
        "match" in m
        or "top" in m
        or (any(k in m for k in ("show", "list", "more")) and any(k in m for k in dwelling_words))
    ):
        return "matches"
    if "save" in m and "listing" in m:
        return "save"
    if "explain" in m:
        return "explain"
    if "draft" in m:
        return "draft"
    if "since last" in m or "what changed" in m:
        return "what_changed"
    return "fallback"


_listing_id_pat = re.compile(r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b")


@router.post("/chat", response_model=ChatOut)
async def chat(
    payload: ChatIn,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> ChatOut:
    if payload.conversation_session_id:
        session = (
            db.query(ConversationSession)
            .filter(
                ConversationSession.id == payload.conversation_session_id,
                ConversationSession.user_id == current_user.id,
            )
            .first()
        )
    else:
        session = None
    if session is None:
        session = ConversationSession(user_id=current_user.id, channel="chat")
        db.add(session)
        db.flush()

    db.add(
        ConversationMessage(
            conversation_session_id=session.id,
            user_id=current_user.id,
            role="user",
            content=payload.message,
        )
    )

    intent = _intent(payload.message)
    tool_calls: list[dict] = []
    reply: str

    if intent in ("greet", "noop", "fallback"):
        reply = _greet_or_status(db, current_user.id)
    elif intent == "edit_profile":
        tool_calls.append({"name": "open_profile_editor", "result": {"ok": True}})
        reply = "Sure — I opened your search profile so you can update and reconfirm it."
    elif intent == "confirm":
        res = assistant_tools.confirm_profile(db, current_user.id)
        tool_calls.append({"name": "confirm_profile", "result": res})
        if res.get("ok"):
            reply = "Profile confirmed. You can run a search whenever you're ready."
        else:
            missing = res.get("missing_fields") or []
            reply = "Can't confirm yet — still missing: " + ", ".join(missing) + "."
    elif intent == "search":
        res = await assistant_tools.run_apartment_search(
            db, current_user.id, max_results=10, allow_unconfirmed_profile=False
        )
        tool_calls.append({"name": "run_apartment_search", "result": res})
        if not res.get("ok"):
            reply = res.get("message") or "Search could not be run."
        else:
            top = res.get("matches") or []
            if not top:
                reply = "Search complete — no matches yet."
            else:
                top3 = top[:3]
                lines = [f"Found {res['result_count']} matches. Top 3:"]
                for m in top3:
                    l = m["listing"]
                    lines.append(
                        f"• {l['title']} — €{l.get('total_monthly_eur') or l.get('rent_eur') or '?'} "
                        f"— score {m['overall_score']}/100"
                    )
                reply = "\n".join(lines)
    elif intent == "matches":
        res = assistant_tools.list_top_matches(db, current_user.id, limit=5)
        tool_calls.append({"name": "list_top_matches", "result": res})
        ms = res.get("matches") or []
        if not ms:
            reply = "No matches yet — run a search."
        else:
            reply = "\n".join(
                f"• {m['listing']['title']} — score {m['overall_score']}/100" for m in ms
            )
    elif intent == "save":
        m = _listing_id_pat.search(payload.message)
        if not m:
            reply = "I need a listing id to save (e.g. save listing <id>)."
        else:
            res = assistant_tools.save_listing(db, current_user.id, m.group(1))
            tool_calls.append({"name": "save_listing", "result": res})
            reply = "Saved." if res.get("ok") else "Couldn't save that listing."
    elif intent == "explain":
        m = _listing_id_pat.search(payload.message)
        if not m:
            reply = "I need a listing id to explain (e.g. explain <id>)."
        else:
            res = assistant_tools.explain_listing(db, current_user.id, m.group(1))
            tool_calls.append({"name": "explain_listing", "result": res})
            reply = res.get("explanation") or "Couldn't explain that listing."
    elif intent == "draft":
        m = _listing_id_pat.search(payload.message)
        lang = "fr" if " fr" in payload.message.lower() or "french" in payload.message.lower() else "en"
        if not m:
            reply = "I need a listing id to draft (e.g. draft <id>)."
        else:
            res = assistant_tools.draft_viewing_request(db, current_user.id, m.group(1), lang)
            tool_calls.append({"name": "draft_viewing_request", "result": res})
            if res.get("ok"):
                draft = res.get("draft", {})
                reply = (
                    f"Draft ({draft.get('language')}):\n"
                    f"Subject: {draft.get('subject')}\n\n{draft.get('body')}"
                )
            else:
                reply = "Couldn't draft for that listing."
    elif intent == "what_changed":
        res = assistant_tools.what_changed_since_last_visit(db, current_user.id)
        tool_calls.append({"name": "what_changed_since_last_visit", "result": res})
        reply = (
            f"Last search at {res.get('last_search_at') or 'never'}, "
            f"{res.get('last_search_result_count') or 0} matches, "
            f"{res.get('saved_listings_count') or 0} saved, "
            f"{res.get('drafts_count') or 0} draft(s)."
        )
    else:
        reply = _greet_or_status(db, current_user.id)

    db.add(
        ConversationMessage(
            conversation_session_id=session.id,
            user_id=current_user.id,
            role="assistant",
            content=reply,
            meta={"tool_calls": tool_calls} if tool_calls else None,
        )
    )
    session.updated_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(session)

    return ChatOut(
        conversation_session_id=session.id,
        reply=reply,
        tool_calls=tool_calls,
    )
