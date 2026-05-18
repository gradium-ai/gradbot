"""Account-scoped tool functions used by REST chat, voice, and tests.

All public functions take a SQLAlchemy session and a user_id (string). They
return dicts shaped for both API responses and LLM tool results.

Centralizing behavior here ensures voice and chat share identical business
logic.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ..models import (
    AlertPreference,
    Listing,
    ListingMatch,
    ProfileIntakeSession,
    RenterProfile,
    SavedListing,
    SearchProfile,
    SearchRun,
    User,
    ViewingRequestDraft,
)
from .drafting import draft_viewing_request_text
from .normalize import english_listing_title, safe_external_url
from .requirement_extraction import compute_missing_fields, extract_requirements
from .search_pipeline import run_search_for_user

logger = logging.getLogger(__name__)


# ─────────────────────── helpers ───────────────────────
SEARCH_PROFILE_FIELDS = (
    "name",
    "is_active",
    "max_rent_including_charges_eur",
    "min_bedrooms",
    "min_rooms",
    "min_surface_m2",
    "furnished_preference",
    "commute_max_minutes",
    "commute_modes",
    "commute_logic",
    "preferred_arrondissements",
    "excluded_arrondissements",
    "room_requirements",
    "nearby_requirements",
)

RENTER_PROFILE_FIELDS = (
    "display_name",
    "preferred_language",
    "phone",
    "dossierfacile_url",
    "work_location_label",
    "work_location_address",
    "work_lat",
    "work_lon",
)

_INT_LIMITS: dict[str, tuple[int, int]] = {
    "max_rent_including_charges_eur": (1, 50_000),
    "min_bedrooms": (0, 10),
    "min_rooms": (1, 20),
    "min_surface_m2": (1, 500),
    "commute_max_minutes": (1, 180),
}

_CLEARABLE_SEARCH_FIELDS = {
    "max_rent_including_charges_eur",
    "min_bedrooms",
    "min_rooms",
    "min_surface_m2",
    "furnished_preference",
}

_ARRONDISSEMENT_FIELDS = {
    "preferred_arrondissements",
    "excluded_arrondissements",
}


_WORK_LOCATION_ALIASES = {
    "work_location",
    "workplace",
    "work_address",
    "workplace_address",
    "office_address",
}

_SEARCH_FIELD_ALIASES = {
    "budget": "max_rent_including_charges_eur",
    "max_budget": "max_rent_including_charges_eur",
    "maximum_budget": "max_rent_including_charges_eur",
    "max_rent": "max_rent_including_charges_eur",
    "maximum_rent": "max_rent_including_charges_eur",
    "rent_budget": "max_rent_including_charges_eur",
    "bedrooms": "min_bedrooms",
    "minimum_bedrooms": "min_bedrooms",
    "min_bedroom": "min_bedrooms",
    "minimum_bedroom": "min_bedrooms",
    "rooms": "min_rooms",
    "minimum_rooms": "min_rooms",
    "min_room": "min_rooms",
    "minimum_room": "min_rooms",
    "min_surface": "min_surface_m2",
    "minimum_surface": "min_surface_m2",
    "min_surface_area": "min_surface_m2",
    "minimum_surface_area": "min_surface_m2",
    "min_surface_area_m2": "min_surface_m2",
    "minimum_surface_area_m2": "min_surface_m2",
    "min_surface_area_sqm": "min_surface_m2",
    "minimum_surface_area_sqm": "min_surface_m2",
    "surface_area": "min_surface_m2",
    "surface_area_m2": "min_surface_m2",
    "surface_area_sqm": "min_surface_m2",
    "arrondissements": "preferred_arrondissements",
    "preferred_arrondissement": "preferred_arrondissements",
    "preferred_areas": "preferred_arrondissements",
    "excluded_arrondissement": "excluded_arrondissements",
}


def _looks_like_precise_address(value: Any) -> bool:
    text = f" {str(value or '').lower().strip()} "
    return bool(
        any(ch.isdigit() for ch in text)
        or any(
            word in text
            for word in (
                " rue ",
                " avenue ",
                " boulevard ",
                " blvd ",
                " street ",
                " st ",
                " road ",
                " rd ",
                " place ",
                " quai ",
                " paris",
                "750",
            )
        )
    )


def _normalize_profile_patch_aliases(patch: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(patch or {})
    for alias in _WORK_LOCATION_ALIASES:
        value = normalized.pop(alias, None)
        if value in (None, ""):
            continue
        target = (
            "work_location_address"
            if _looks_like_precise_address(value)
            else "work_location_label"
        )
        normalized.setdefault(target, value)
    for alias, target in _SEARCH_FIELD_ALIASES.items():
        value = normalized.pop(alias, None)
        if value in (None, ""):
            continue
        normalized.setdefault(target, value)
    return normalized


def _coerce_nearby_requirements(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, list):
        return {}

    defaults = {
        "supermarket": ("supermarket_m", 500),
        "grocery": ("supermarket_m", 500),
        "metro": ("metro_m", 700),
        "station": ("metro_m", 700),
        "park": ("park_m", 1000),
        "hospital": ("hospital_m", 2000),
        "gym": ("gym_m", 1500),
        "school": ("school_m", 1500),
        "pharmacy": ("pharmacy_m", 700),
        "bakery": ("bakery_m", 500),
    }
    out: dict[str, Any] = {}
    for item in value:
        key = str(item).lower().strip()
        mapped = defaults.get(key)
        if mapped:
            out[mapped[0]] = mapped[1]
    return out


def _merge_room_requirements(current: Any, patch: Any) -> dict[str, Any]:
    rooms: dict[str, Any] = {}
    if isinstance(current, dict):
        rooms = {
            room: {
                "must_have": list((spec or {}).get("must_have") or []),
                "nice_to_have": list((spec or {}).get("nice_to_have") or []),
            }
            for room, spec in current.items()
            if isinstance(spec, dict)
        }
    if not isinstance(patch, dict):
        return rooms

    for room, spec in patch.items():
        if not isinstance(spec, dict):
            continue
        target = rooms.setdefault(room, {"must_have": [], "nice_to_have": []})
        for kind in ("must_have", "nice_to_have"):
            values = spec.get(kind) or []
            if isinstance(values, str):
                values = [values]
            for value in values:
                label = str(value).strip()
                if label and label not in target[kind]:
                    target[kind].append(label)
    return rooms


def _coerce_int_field(key: str, value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        coerced = int(value)
    except (TypeError, ValueError):
        return None
    low, high = _INT_LIMITS[key]
    if low <= coerced <= high:
        return coerced
    return None


def _coerce_arrondissement_list(value: Any) -> list[int]:
    if value is None:
        return []

    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: list[int] = []

    def add(candidate: int) -> None:
        if 1 <= candidate <= 20 and candidate not in out:
            out.append(candidate)

    for item in items:
        if isinstance(item, int):
            add(item - 75000 if 75001 <= item <= 75020 else item)
            continue

        text = str(item or "").lower()
        if not text:
            continue

        for postal in re.findall(r"\b750(0[1-9]|1[0-9]|20)\b", text):
            add(int(postal))

        for ordinal in re.findall(r"\b([1-9]|1[0-9]|20)(?:st|nd|rd|th|e|er|eme|ème)?\b", text):
            add(int(ordinal))

    return out


def repair_search_profile(sp: SearchProfile) -> bool:
    """Normalize persisted profile values that predate current validation."""
    changed = False
    for key in _INT_LIMITS:
        value = getattr(sp, key)
        if value is None:
            continue
        repaired = _coerce_int_field(key, value)
        if repaired is None:
            repaired = 30 if key == "commute_max_minutes" else None
        if repaired != value:
            setattr(sp, key, repaired)
            changed = True

    if sp.furnished_preference not in (None, "required", "prefer", "any"):
        sp.furnished_preference = None
        changed = True

    for key in _ARRONDISSEMENT_FIELDS:
        value = getattr(sp, key)
        repaired = _coerce_arrondissement_list(value)
        if repaired != (value or []):
            setattr(sp, key, repaired)
            changed = True

    return changed


def _serialize_search_profile(p: SearchProfile, *, renter: Optional[RenterProfile] = None) -> dict[str, Any]:
    repair_search_profile(p)
    out: dict[str, Any] = {
        "id": p.id,
        "user_id": p.user_id,
        "name": p.name,
        "is_active": p.is_active,
        "confirmation_status": p.confirmation_status,
        "last_confirmed_at": p.last_confirmed_at.isoformat() if p.last_confirmed_at else None,
        "max_rent_including_charges_eur": p.max_rent_including_charges_eur,
        "min_bedrooms": p.min_bedrooms,
        "min_rooms": p.min_rooms,
        "min_surface_m2": p.min_surface_m2,
        "furnished_preference": p.furnished_preference,
        "commute_max_minutes": p.commute_max_minutes,
        "commute_modes": p.commute_modes or [],
        "commute_logic": p.commute_logic,
        "preferred_arrondissements": p.preferred_arrondissements or [],
        "excluded_arrondissements": p.excluded_arrondissements or [],
        "room_requirements": p.room_requirements or {},
        "nearby_requirements": _coerce_nearby_requirements(p.nearby_requirements),
    }
    if renter is not None:
        out["work_location_label"] = renter.work_location_label
        out["work_location_address"] = renter.work_location_address
    return out


def _serialize_listing(l: Listing) -> dict[str, Any]:
    return {
        "id": l.id,
        "canonical_url": safe_external_url(l.canonical_url or ""),
        "source": l.source,
        "title": english_listing_title(l.title),
        "description": l.description,
        "rent_eur": l.rent_eur,
        "charges_eur": l.charges_eur,
        "total_monthly_eur": l.total_monthly_eur,
        "surface_m2": l.surface_m2,
        "rooms": l.rooms,
        "bedrooms": l.bedrooms,
        "furnished": l.furnished,
        "address_text": l.address_text,
        "arrondissement": l.arrondissement,
        "features": l.features or [],
        "missing_fields": l.missing_fields or [],
        "is_mock": l.is_mock,
    }


def _get_or_create_renter_profile(db: Session, user_id: str) -> RenterProfile:
    rp = db.query(RenterProfile).filter(RenterProfile.user_id == user_id).first()
    if rp is None:
        rp = RenterProfile(user_id=user_id)
        db.add(rp)
        db.flush()
    return rp


def get_active_search_profile(db: Session, user_id: str) -> Optional[SearchProfile]:
    return (
        db.query(SearchProfile)
        .filter(SearchProfile.user_id == user_id, SearchProfile.is_active.is_(True))
        .order_by(SearchProfile.created_at.desc())
        .first()
    )


def _get_or_create_search_profile(db: Session, user_id: str) -> SearchProfile:
    sp = get_active_search_profile(db, user_id)
    if sp is None:
        sp = SearchProfile(user_id=user_id)
        db.add(sp)
        db.flush()
    return sp


def _get_or_create_intake(db: Session, user_id: str, search_profile: SearchProfile) -> ProfileIntakeSession:
    intake = (
        db.query(ProfileIntakeSession)
        .filter(
            ProfileIntakeSession.user_id == user_id,
            ProfileIntakeSession.status.in_(("collecting", "review_required")),
        )
        .order_by(ProfileIntakeSession.created_at.desc())
        .first()
    )
    if intake is None:
        intake = ProfileIntakeSession(
            user_id=user_id,
            search_profile_id=search_profile.id,
            status="collecting",
            channel="voice_text",
        )
        db.add(intake)
        db.flush()
    return intake


def _profile_dict_for_extraction(sp: SearchProfile, rp: RenterProfile) -> dict[str, Any]:
    d = _serialize_search_profile(sp, renter=rp)
    d["work_location_label"] = rp.work_location_label
    d["work_location_address"] = rp.work_location_address
    return d


def _apply_patch_to_search_profile(
    sp: SearchProfile, patch: dict[str, Any], *, allowed: tuple[str, ...] = SEARCH_PROFILE_FIELDS
) -> dict[str, str]:
    """Apply the dict patch to the SearchProfile. Returns a map of {field: source}."""
    sources: dict[str, str] = {}
    for key, value in patch.items():
        if key not in allowed:
            continue

        if key in _INT_LIMITS:
            value = _coerce_int_field(key, value)
            if value is None and key not in _CLEARABLE_SEARCH_FIELDS:
                continue
        elif value is None:
            if key not in _CLEARABLE_SEARCH_FIELDS:
                continue
        if key == "furnished_preference" and value not in (None, "required", "prefer", "any"):
            continue
        if key == "nearby_requirements":
            value = _coerce_nearby_requirements(value)
        if key == "room_requirements":
            value = _merge_room_requirements(getattr(sp, key), value)
        if key in _ARRONDISSEMENT_FIELDS:
            value = _coerce_arrondissement_list(value)
        setattr(sp, key, value)
        sources[key] = "voice"
    return sources


def _apply_patch_to_renter(
    rp: RenterProfile, patch: dict[str, Any], *, allowed: tuple[str, ...] = RENTER_PROFILE_FIELDS
) -> dict[str, str]:
    sources: dict[str, str] = {}
    for key, value in patch.items():
        if key not in allowed:
            continue
        if value is None:
            continue
        setattr(rp, key, value)
        sources[key] = "voice"
    return sources


# ─────────────────────── tools ───────────────────────
def start_profile_intake(db: Session, user_id: str) -> dict[str, Any]:
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    intake = _get_or_create_intake(db, user_id, sp)
    db.commit()
    db.refresh(intake)

    profile_dict = _profile_dict_for_extraction(sp, rp)
    missing = compute_missing_fields(profile_dict)
    return {
        "ok": True,
        "intake_session_id": intake.id,
        "status": intake.status,
        "raw_transcript": intake.raw_transcript,
        "draft_profile": profile_dict,
        "missing_fields": missing,
        "ambiguous_fields": intake.ambiguous_fields or [],
        "field_confidence": intake.field_confidence or {},
        "field_sources": intake.field_sources or {},
        "confirmation_status": sp.confirmation_status,
    }


def get_profile_draft(db: Session, user_id: str) -> dict[str, Any]:
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    intake = _get_or_create_intake(db, user_id, sp)
    db.commit()
    db.refresh(intake)
    profile_dict = _profile_dict_for_extraction(sp, rp)
    return {
        "ok": True,
        "intake_session_id": intake.id,
        "status": intake.status,
        "raw_transcript": intake.raw_transcript,
        "draft_profile": profile_dict,
        "missing_fields": compute_missing_fields(profile_dict),
        "ambiguous_fields": intake.ambiguous_fields or [],
        "field_confidence": intake.field_confidence or {},
        "field_sources": intake.field_sources or {},
        "confirmation_status": sp.confirmation_status,
    }


def extract_requirements_from_transcript(
    db: Session, user_id: str, transcript: str, *, source: str = "voice"
) -> dict[str, Any]:
    """Extract requirements from transcript and update the draft search profile."""
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    intake = _get_or_create_intake(db, user_id, sp)

    existing = _profile_dict_for_extraction(sp, rp)
    extracted = extract_requirements(transcript, existing_profile=existing)
    patch = extracted.profile_patch

    # Apply work location to renter profile
    work_patch: dict[str, Any] = {}
    if "work_location_label" in patch:
        work_patch["work_location_label"] = patch["work_location_label"]
    if "work_location_address" in patch:
        work_patch["work_location_address"] = patch["work_location_address"]
    if work_patch:
        _apply_patch_to_renter(rp, work_patch)

    # Apply remainder to search profile
    sp_patch = {k: v for k, v in patch.items() if k not in ("work_location_label", "work_location_address")}
    _apply_patch_to_search_profile(sp, sp_patch)

    # Update intake session
    intake.raw_transcript = (intake.raw_transcript or "") + "\n" + transcript if intake.raw_transcript else transcript
    intake.latest_user_text = transcript
    intake.extracted_profile_patch = patch
    sources = dict(intake.field_sources or {})
    for k in patch:
        sources[k] = source
    intake.field_sources = sources

    new_conf = dict(intake.field_confidence or {})
    new_conf.update(extracted.field_confidence)
    intake.field_confidence = new_conf

    profile_dict = _profile_dict_for_extraction(sp, rp)
    missing = compute_missing_fields(profile_dict)
    intake.missing_fields = missing
    intake.ambiguous_fields = extracted.ambiguous_fields
    intake.status = "review_required" if not missing else "collecting"
    db.commit()
    db.refresh(intake)
    db.refresh(sp)
    db.refresh(rp)

    return {
        "ok": True,
        "intake_session_id": intake.id,
        "draft_profile": _profile_dict_for_extraction(sp, rp),
        "summary": extracted.summary,
        "missing_fields": missing,
        "ambiguous_fields": extracted.ambiguous_fields,
        "field_confidence": intake.field_confidence,
        "field_sources": intake.field_sources,
        "confirmation_status": sp.confirmation_status,
    }


def update_profile_draft(
    db: Session, user_id: str, patch: dict[str, Any], *, source: str = "text"
) -> dict[str, Any]:
    patch = _normalize_profile_patch_aliases(patch)
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    intake = _get_or_create_intake(db, user_id, sp)

    work_patch = {
        k: v for k, v in patch.items() if k in ("work_location_label", "work_location_address", "work_lat", "work_lon")
    }
    if work_patch:
        _apply_patch_to_renter(rp, work_patch)
    sp_patch = {k: v for k, v in patch.items() if k not in work_patch}
    _apply_patch_to_search_profile(sp, sp_patch)

    sources = dict(intake.field_sources or {})
    for k in patch:
        sources[k] = source
    intake.field_sources = sources

    profile_dict = _profile_dict_for_extraction(sp, rp)
    missing = compute_missing_fields(profile_dict)
    intake.missing_fields = missing
    if not missing:
        intake.status = "review_required"
    db.commit()
    db.refresh(intake)
    db.refresh(sp)
    db.refresh(rp)

    return {
        "ok": True,
        "intake_session_id": intake.id,
        "draft_profile": _profile_dict_for_extraction(sp, rp),
        "missing_fields": missing,
        "ambiguous_fields": intake.ambiguous_fields or [],
        "field_confidence": intake.field_confidence or {},
        "field_sources": intake.field_sources or {},
        "confirmation_status": sp.confirmation_status,
    }


def confirm_profile(db: Session, user_id: str) -> dict[str, Any]:
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    intake = _get_or_create_intake(db, user_id, sp)

    profile_dict = _profile_dict_for_extraction(sp, rp)
    missing = compute_missing_fields(profile_dict)
    if missing:
        return {
            "ok": False,
            "error": "missing_required_fields",
            "missing_fields": missing,
            "draft_profile": profile_dict,
            "message": "Some required fields are still missing.",
        }

    sp.confirmation_status = "confirmed"
    sp.last_confirmed_at = datetime.now(timezone.utc)
    intake.status = "confirmed"
    intake.confirmed_at = datetime.now(timezone.utc)
    intake.confirmed_profile_snapshot = profile_dict
    db.commit()
    db.refresh(sp)
    db.refresh(intake)

    return {
        "ok": True,
        "search_profile": _serialize_search_profile(sp, renter=rp),
        "intake_session_id": intake.id,
        "message": "Profile confirmed. You can now run a search.",
    }


def get_user_context(db: Session, user_id: str) -> dict[str, Any]:
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        return {"ok": False, "error": "user_not_found"}
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)

    saved_count = db.query(SavedListing).filter(
        SavedListing.user_id == user_id, SavedListing.status == "saved"
    ).count()
    rejected_count = db.query(SavedListing).filter(
        SavedListing.user_id == user_id, SavedListing.status == "rejected"
    ).count()
    drafts_count = db.query(ViewingRequestDraft).filter(ViewingRequestDraft.user_id == user_id).count()
    last_run = (
        db.query(SearchRun)
        .filter(SearchRun.user_id == user_id)
        .order_by(SearchRun.started_at.desc())
        .first()
    )
    profile_dict = _profile_dict_for_extraction(sp, rp)

    return {
        "ok": True,
        "user": {
            "id": user.id,
            "email": user.email,
            "full_name": user.full_name,
        },
        "renter_profile": {
            "display_name": rp.display_name,
            "preferred_language": rp.preferred_language,
            "phone": rp.phone,
            "work_location_label": rp.work_location_label,
            "work_location_address": rp.work_location_address,
        },
        "search_profile": _serialize_search_profile(sp, renter=rp),
        "confirmation_status": sp.confirmation_status,
        "missing_fields": compute_missing_fields(profile_dict),
        "saved_listings_count": saved_count,
        "rejected_listings_count": rejected_count,
        "drafts_count": drafts_count,
        "last_search_at": last_run.started_at.isoformat() if last_run else None,
        "last_search_result_count": last_run.result_count if last_run else 0,
    }


def update_renter_profile(
    db: Session, user_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    rp = _get_or_create_renter_profile(db, user_id)
    _apply_patch_to_renter(rp, patch)
    db.commit()
    db.refresh(rp)
    return {
        "ok": True,
        "renter_profile": {
            "id": rp.id,
            "display_name": rp.display_name,
            "preferred_language": rp.preferred_language,
            "phone": rp.phone,
            "work_location_label": rp.work_location_label,
            "work_location_address": rp.work_location_address,
        },
    }


def update_search_profile(
    db: Session, user_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    _apply_patch_to_search_profile(sp, patch)
    db.commit()
    db.refresh(sp)
    return {"ok": True, "search_profile": _serialize_search_profile(sp, renter=rp)}


async def run_apartment_search(
    db: Session,
    user_id: str,
    *,
    max_results: int = 20,
    refresh: bool = True,
    allow_unconfirmed_profile: bool = False,
) -> dict[str, Any]:
    """Wrapper around the search pipeline. Returns top matches with listing data."""
    result = await run_search_for_user(
        db,
        user_id,
        max_results=max_results,
        allow_unconfirmed_profile=allow_unconfirmed_profile,
    )
    if not result.get("ok"):
        return result

    # Hydrate matches with listing snippets
    listing_ids = [m["listing_id"] for m in result["matches"]]
    listings = db.query(Listing).filter(Listing.id.in_(listing_ids)).all() if listing_ids else []
    by_id = {l.id: l for l in listings}
    matches = []
    for m in result["matches"]:
        l = by_id.get(m["listing_id"])
        if not l:
            continue
        matches.append({**m, "listing": _serialize_listing(l)})
    return {
        "ok": True,
        "search_run_id": result["search_run_id"],
        "result_count": result["result_count"],
        "matches": matches,
    }


def _profile_updated_after_search(
    search_run: SearchRun,
    search_profile: SearchProfile | None,
    renter_profile: RenterProfile | None,
) -> bool:
    if search_profile is None:
        return False
    run_started_at = search_run.started_at
    profile_timestamps = [
        ts
        for ts in (
            search_profile.updated_at,
            renter_profile.updated_at if renter_profile else None,
        )
        if ts is not None
    ]
    if not run_started_at or not profile_timestamps:
        return False

    def normalized(dt: datetime) -> datetime:
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt

    latest_profile_update = max(normalized(ts) for ts in profile_timestamps)
    return normalized(run_started_at) < latest_profile_update


def list_top_matches(db: Session, user_id: str, *, limit: int = 20) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    active_profile = get_active_search_profile(db, user_id)
    last_run = (
        db.query(SearchRun)
        .filter(SearchRun.user_id == user_id, SearchRun.status == "completed")
        .order_by(SearchRun.started_at.desc())
        .first()
    )
    if not last_run:
        return {"ok": True, "matches": [], "message": "No completed searches yet."}

    renter_profile = db.query(RenterProfile).filter(RenterProfile.user_id == user_id).first()
    if _profile_updated_after_search(last_run, active_profile, renter_profile):
        return {
            "ok": True,
            "matches": [],
            "search_run_id": last_run.id,
            "stale": True,
            "message": "Profile changed after the latest search. Run a fresh search.",
        }

    rejected_listing_ids = {
        sl.listing_id
        for sl in db.query(SavedListing).filter(
            SavedListing.user_id == user_id, SavedListing.status == "rejected"
        ).all()
    }

    matches = (
        db.query(ListingMatch)
        .filter(
            ListingMatch.user_id == user_id,
            ListingMatch.search_run_id == last_run.id,
        )
        .order_by(ListingMatch.overall_score.desc())
        .limit(limit * 2)
        .all()
    )
    out: list[dict[str, Any]] = []
    listing_ids = [m.listing_id for m in matches]
    listings = {l.id: l for l in db.query(Listing).filter(Listing.id.in_(listing_ids)).all()}
    for m in matches:
        if m.listing_id in rejected_listing_ids:
            continue
        l = listings.get(m.listing_id)
        if not l:
            continue
        out.append({
            "match_id": m.id,
            "listing_id": l.id,
            "overall_score": m.overall_score,
            "passes_hard_filters": m.passes_hard_filters,
            "reasons": m.reasons,
            "warnings": m.warnings,
            "commute": m.commute,
            "listing": _serialize_listing(l),
        })
        if len(out) >= limit:
            break
    return {"ok": True, "matches": out, "search_run_id": last_run.id}


def explain_listing(db: Session, user_id: str, listing_id: str) -> dict[str, Any]:
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        return {"ok": False, "error": "listing_not_found"}
    match = (
        db.query(ListingMatch)
        .filter(
            ListingMatch.user_id == user_id,
            ListingMatch.listing_id == listing_id,
        )
        .order_by(ListingMatch.created_at.desc())
        .first()
    )
    if not match:
        return {
            "ok": True,
            "listing": _serialize_listing(listing),
            "explanation": (
                "I haven't scored this listing for you yet. Run a fresh search to see how it matches "
                "your profile."
            ),
        }
    parts = [
        f"This listing scores {match.overall_score}/100 against your profile.",
    ]
    if not match.passes_hard_filters:
        parts.append("It does NOT pass your hard filters: " + "; ".join(match.reasons[:3]))
    else:
        if match.reasons:
            parts.append("Why it could fit: " + "; ".join(match.reasons[:5]) + ".")
    if match.warnings:
        parts.append("Caveats: " + "; ".join(match.warnings) + ".")
    if listing.is_mock:
        parts.append("(Note: this is a mock listing — TAVILY_API_KEY is not set.)")

    return {
        "ok": True,
        "listing": _serialize_listing(listing),
        "match": {
            "overall_score": match.overall_score,
            "passes_hard_filters": match.passes_hard_filters,
            "reasons": match.reasons,
            "warnings": match.warnings,
            "commute": match.commute,
        },
        "explanation": " ".join(parts),
    }


def save_listing(db: Session, user_id: str, listing_id: str) -> dict[str, Any]:
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        return {"ok": False, "error": "listing_not_found"}
    sl = (
        db.query(SavedListing)
        .filter(SavedListing.user_id == user_id, SavedListing.listing_id == listing_id)
        .first()
    )
    if sl:
        sl.status = "saved"
    else:
        sl = SavedListing(user_id=user_id, listing_id=listing_id, status="saved")
        db.add(sl)
    db.commit()
    db.refresh(sl)
    return {"ok": True, "saved_listing_id": sl.id, "listing_id": listing_id}


def reject_listing(db: Session, user_id: str, listing_id: str, reason: str | None = None) -> dict[str, Any]:
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        return {"ok": False, "error": "listing_not_found"}
    sl = (
        db.query(SavedListing)
        .filter(SavedListing.user_id == user_id, SavedListing.listing_id == listing_id)
        .first()
    )
    if sl:
        sl.status = "rejected"
        if reason:
            sl.notes = reason
    else:
        sl = SavedListing(user_id=user_id, listing_id=listing_id, status="rejected", notes=reason)
        db.add(sl)
    db.commit()
    db.refresh(sl)
    return {"ok": True, "saved_listing_id": sl.id, "listing_id": listing_id}


def list_saved_listings(db: Session, user_id: str, *, limit: int = 50) -> dict[str, Any]:
    limit = max(1, min(limit, 50))
    rows = (
        db.query(SavedListing)
        .filter(SavedListing.user_id == user_id, SavedListing.status != "rejected")
        .order_by(SavedListing.updated_at.desc())
        .limit(limit)
        .all()
    )
    listings = {
        l.id: l
        for l in db.query(Listing).filter(Listing.id.in_([r.listing_id for r in rows])).all()
    }
    out = []
    for r in rows:
        l = listings.get(r.listing_id)
        if not l:
            continue
        out.append({
            "id": r.id,
            "listing_id": r.listing_id,
            "status": r.status,
            "notes": r.notes,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "listing": _serialize_listing(l),
        })
    return {"ok": True, "saved_listings": out}


def update_saved_listing(
    db: Session, user_id: str, saved_listing_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    row = (
        db.query(SavedListing)
        .filter(SavedListing.user_id == user_id, SavedListing.id == saved_listing_id)
        .first()
    )
    if not row:
        return {"ok": False, "error": "saved_listing_not_found"}
    if "status" in patch and patch["status"]:
        row.status = patch["status"]
    if "notes" in patch:
        row.notes = patch["notes"]
    db.commit()
    db.refresh(row)
    return {
        "ok": True,
        "saved_listing": {
            "id": row.id,
            "listing_id": row.listing_id,
            "status": row.status,
            "notes": row.notes,
        },
    }


def draft_viewing_request(
    db: Session, user_id: str, listing_id: str, language: str = "en"
) -> dict[str, Any]:
    listing = db.query(Listing).filter(Listing.id == listing_id).first()
    if not listing:
        return {"ok": False, "error": "listing_not_found"}
    user = db.query(User).filter(User.id == user_id).first()
    rp = _get_or_create_renter_profile(db, user_id)
    sp = _get_or_create_search_profile(db, user_id)

    subject, body = draft_viewing_request_text(
        user_full_name=user.full_name if user else None,
        renter_phone=rp.phone,
        listing=_serialize_listing(listing),
        profile=_serialize_search_profile(sp, renter=rp),
        language=language if language in ("en", "fr") else "en",
    )
    draft = ViewingRequestDraft(
        user_id=user_id,
        listing_id=listing_id,
        language=language if language in ("en", "fr") else "en",
        subject=subject,
        body=body,
        status="draft",
    )
    db.add(draft)
    db.commit()
    db.refresh(draft)
    return {
        "ok": True,
        "draft": {
            "id": draft.id,
            "listing_id": draft.listing_id,
            "language": draft.language,
            "subject": draft.subject,
            "body": draft.body,
            "status": draft.status,
        },
        "message": "Draft created. Sending is not implemented in this MVP.",
    }


def what_changed_since_last_visit(db: Session, user_id: str) -> dict[str, Any]:
    last_run = (
        db.query(SearchRun)
        .filter(SearchRun.user_id == user_id, SearchRun.status == "completed")
        .order_by(SearchRun.started_at.desc())
        .first()
    )
    last_run_at = last_run.started_at.isoformat() if last_run else None

    top_match = None
    if last_run:
        m = (
            db.query(ListingMatch)
            .filter(ListingMatch.user_id == user_id, ListingMatch.search_run_id == last_run.id)
            .order_by(ListingMatch.overall_score.desc())
            .first()
        )
        if m:
            l = db.query(Listing).filter(Listing.id == m.listing_id).first()
            if l:
                top_match = {
                    "listing": _serialize_listing(l),
                    "overall_score": m.overall_score,
                }

    saved = db.query(SavedListing).filter(
        SavedListing.user_id == user_id, SavedListing.status == "saved"
    ).count()
    drafts = db.query(ViewingRequestDraft).filter(ViewingRequestDraft.user_id == user_id).count()
    sp = _get_or_create_search_profile(db, user_id)
    rp = _get_or_create_renter_profile(db, user_id)
    missing = compute_missing_fields(_profile_dict_for_extraction(sp, rp))

    return {
        "ok": True,
        "last_search_at": last_run_at,
        "last_search_result_count": last_run.result_count if last_run else 0,
        "top_match": top_match,
        "saved_listings_count": saved,
        "missing_required_fields": missing,
        "drafts_count": drafts,
        "confirmation_status": sp.confirmation_status,
    }


def list_viewing_drafts(db: Session, user_id: str) -> dict[str, Any]:
    rows = (
        db.query(ViewingRequestDraft)
        .filter(ViewingRequestDraft.user_id == user_id)
        .order_by(ViewingRequestDraft.created_at.desc())
        .all()
    )
    return {
        "ok": True,
        "drafts": [
            {
                "id": r.id,
                "listing_id": r.listing_id,
                "language": r.language,
                "subject": r.subject,
                "body": r.body,
                "status": r.status,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }
