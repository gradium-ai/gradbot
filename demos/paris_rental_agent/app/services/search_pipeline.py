"""End-to-end search pipeline: profile → Tavily/mock → normalize → score → persist."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.orm import Session

from ..models import Listing, ListingMatch, RenterProfile, SearchProfile, SearchRun
from .commute import compute_commute_batch
from .normalize import normalize_result
from .requirement_extraction import compute_missing_fields
from .scoring import score_listing
from .tavily_search import (
    TavilyNotConfiguredError,
    TavilySearchError,
    search_paris_rentals,
)

logger = logging.getLogger(__name__)


def _profile_to_dict(p: SearchProfile, renter: RenterProfile | None = None) -> dict[str, Any]:
    return {
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
        "nearby_requirements": p.nearby_requirements or {},
        "work_location_address": renter.work_location_address if renter else None,
        "work_location_label": renter.work_location_label if renter else None,
    }


def _upsert_listing(db: Session, normalized: dict[str, Any]) -> Listing:
    existing = db.query(Listing).filter(Listing.url_hash == normalized["url_hash"]).first()
    now = datetime.now(timezone.utc)
    if existing:
        for k in (
            "canonical_url",
            "source",
            "title",
            "description",
            "rent_eur",
            "charges_eur",
            "total_monthly_eur",
            "surface_m2",
            "rooms",
            "bedrooms",
            "furnished",
            "address_text",
            "arrondissement",
            "features",
            "missing_fields",
            "raw_text",
            "raw_data",
            "is_mock",
        ):
            v = normalized.get(k)
            if v is not None:
                setattr(existing, k, v)
        existing.last_seen_at = now
        return existing

    listing = Listing(
        canonical_url=normalized.get("canonical_url"),
        url_hash=normalized["url_hash"],
        source=normalized.get("source"),
        title=normalized.get("title") or "Untitled",
        description=normalized.get("description"),
        rent_eur=normalized.get("rent_eur"),
        charges_eur=normalized.get("charges_eur"),
        total_monthly_eur=normalized.get("total_monthly_eur"),
        surface_m2=normalized.get("surface_m2"),
        rooms=normalized.get("rooms"),
        bedrooms=normalized.get("bedrooms"),
        furnished=normalized.get("furnished"),
        address_text=normalized.get("address_text"),
        arrondissement=normalized.get("arrondissement"),
        features=normalized.get("features") or [],
        missing_fields=normalized.get("missing_fields") or [],
        raw_text=normalized.get("raw_text"),
        raw_data=normalized.get("raw_data"),
        is_mock=bool(normalized.get("is_mock")),
        first_seen_at=now,
        last_seen_at=now,
    )
    db.add(listing)
    db.flush()
    return listing


async def run_search_for_user(
    db: Session,
    user_id: str,
    *,
    max_results: int = 20,
    allow_unconfirmed_profile: bool = False,
) -> dict[str, Any]:
    """Run a search for the user, persist results, return top matches.

    Returns a structured dict so callers (REST + voice tool) get the same shape.
    """
    max_results = max(1, min(max_results, 50))
    profile = (
        db.query(SearchProfile)
        .filter(SearchProfile.user_id == user_id, SearchProfile.is_active.is_(True))
        .order_by(SearchProfile.created_at.desc())
        .first()
    )
    if not profile:
        return {
            "ok": False,
            "error": "no_search_profile",
            "message": "No active search profile found.",
        }

    if profile.confirmation_status != "confirmed" and not allow_unconfirmed_profile:
        return {
            "ok": False,
            "error": "search_profile_not_confirmed",
            "message": "Please review and confirm your search profile before running a search.",
        }

    renter = (
        db.query(RenterProfile).filter(RenterProfile.user_id == user_id).first()
    )
    profile_dict = _profile_to_dict(profile, renter)
    missing = compute_missing_fields(profile_dict)
    if missing:
        return {
            "ok": False,
            "error": "search_profile_incomplete",
            "message": "Missing required fields before search.",
            "missing_fields": missing,
        }

    run = SearchRun(
        user_id=user_id,
        search_profile_id=profile.id,
        status="running",
        query_payload=profile_dict,
    )
    db.add(run)
    # Commit early so we don't hold a write lock during the long Tavily await.
    # On SQLite this matters a lot — any open transaction blocks other writers.
    db.commit()
    db.refresh(run)

    try:
        try:
            raw_results = await search_paris_rentals(profile_dict)
        except TavilyNotConfiguredError as exc:
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {
                "ok": False,
                "error": "tavily_not_configured",
                "message": str(exc),
                "search_run_id": run.id,
            }
        except TavilySearchError as exc:
            run.status = "failed"
            run.error_message = str(exc)
            run.completed_at = datetime.now(timezone.utc)
            db.commit()
            return {
                "ok": False,
                "error": "tavily_search_failed",
                "message": str(exc),
                "search_run_id": run.id,
            }
        normalized: list[dict[str, Any]] = []
        for r in raw_results:
            try:
                normalized.append(normalize_result(r))
            except Exception:
                logger.exception("Failed to normalize result")

        # Dedupe by url_hash
        seen: set[str] = set()
        unique: list[dict[str, Any]] = []
        for n in normalized:
            if n["url_hash"] in seen:
                continue
            seen.add(n["url_hash"])
            unique.append(n)

        # Batch commute lookup before scoring. Falls back to status="unknown" when
        # no Google Maps key is set, when the workplace is empty, or when an
        # individual address can't be geocoded.
        work_addr = (
            profile_dict.get("work_location_address")
            or profile_dict.get("work_location_label")
            or ""
        )
        commute_inputs: list[str] = []
        for n in unique[:max_results]:
            commute_inputs.append(
                n.get("address_text")
                or (f"Paris {n['arrondissement']:02d}" if n.get("arrondissement") else "")
            )
        commute_by_addr: dict[str, dict[str, Any]] = await compute_commute_batch(
            work_addr, commute_inputs
        )

        match_records: list[dict[str, Any]] = []
        for n, addr in zip(unique[:max_results], commute_inputs):
            listing_row = _upsert_listing(db, n)
            commute = commute_by_addr.get(addr) or {
                "metro_min": None,
                "bike_min": None,
                "walk_min": None,
                "status": "unknown",
            }
            score_data = score_listing(n, profile_dict, commute=commute)
            match = ListingMatch(
                user_id=user_id,
                search_profile_id=profile.id,
                search_run_id=run.id,
                listing_id=listing_row.id,
                overall_score=score_data["overall_score"],
                passes_hard_filters=score_data["passes_hard_filters"],
                reasons=score_data["reasons"],
                warnings=score_data["warnings"],
                commute=score_data["commute"],
            )
            db.add(match)
            db.flush()
            match_records.append(
                {
                    "match_id": match.id,
                    "listing_id": listing_row.id,
                    "overall_score": match.overall_score,
                    "passes_hard_filters": match.passes_hard_filters,
                    "reasons": match.reasons,
                    "warnings": match.warnings,
                    "commute": match.commute,
                }
            )

        match_records.sort(key=lambda m: m["overall_score"], reverse=True)

        run.status = "completed"
        run.result_count = len(match_records)
        run.completed_at = datetime.now(timezone.utc)
        db.commit()

        return {
            "ok": True,
            "search_run_id": run.id,
            "result_count": run.result_count,
            "matches": match_records,
        }

    except Exception as exc:  # pragma: no cover - defensive
        logger.exception("Search pipeline failed")
        run.status = "failed"
        run.error_message = str(exc)
        run.completed_at = datetime.now(timezone.utc)
        db.commit()
        return {
            "ok": False,
            "error": "search_failed",
            "message": str(exc),
            "search_run_id": run.id,
        }
