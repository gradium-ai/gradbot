"""Deterministic scoring of a normalized listing against a SearchProfile."""

from __future__ import annotations

from typing import Any


def _profile_dict(profile: Any) -> dict[str, Any]:
    if isinstance(profile, dict):
        return profile
    out: dict[str, Any] = {}
    for k in (
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
    ):
        out[k] = getattr(profile, k, None)
    return out


def _check_hard_filters(listing: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, list[str]]:
    reasons: list[str] = []
    passes = True

    max_rent = profile.get("max_rent_including_charges_eur")
    total = listing.get("total_monthly_eur") or listing.get("rent_eur")
    if max_rent and total and total > max_rent:
        passes = False
        reasons.append(f"€{total} exceeds budget €{max_rent}")

    if profile.get("min_bedrooms") is not None and listing.get("bedrooms") is not None:
        if listing["bedrooms"] < profile["min_bedrooms"]:
            passes = False
            reasons.append(
                f"{listing['bedrooms']} bedroom(s) is below minimum {profile['min_bedrooms']}"
            )

    if profile.get("min_rooms") is not None and listing.get("rooms") is not None:
        if listing["rooms"] < profile["min_rooms"]:
            passes = False
            reasons.append(f"{listing['rooms']} room(s) is below minimum {profile['min_rooms']}")

    if profile.get("min_surface_m2") and listing.get("surface_m2"):
        if listing["surface_m2"] < profile["min_surface_m2"]:
            passes = False
            reasons.append(
                f"{listing['surface_m2']} m² is below minimum {profile['min_surface_m2']} m²"
            )

    excluded = profile.get("excluded_arrondissements") or []
    if listing.get("arrondissement") in excluded:
        passes = False
        reasons.append(f"arrondissement {listing['arrondissement']} is excluded")

    return passes, reasons


def _budget_fit_score(listing: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    max_rent = profile.get("max_rent_including_charges_eur")
    total = listing.get("total_monthly_eur") or listing.get("rent_eur")
    if not max_rent or not total:
        return 0.5, ["budget unknown"] if not total else []
    ratio = total / max_rent
    if ratio <= 0.85:
        return 1.0, [f"€{total} comfortably under €{max_rent} budget"]
    if ratio <= 1.0:
        return 0.85, [f"€{total} within €{max_rent} budget"]
    return 0.0, [f"€{total} exceeds €{max_rent} budget"]


def _bedroom_room_score(listing: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    reasons: list[str] = []
    if profile.get("min_bedrooms") is not None and listing.get("bedrooms") is not None:
        diff = listing["bedrooms"] - profile["min_bedrooms"]
        if diff >= 0:
            reasons.append(f"{listing['bedrooms']} bedroom(s) meets minimum")
            return 1.0, reasons
        return 0.0, ["bedroom count below minimum"]
    if profile.get("min_rooms") is not None and listing.get("rooms") is not None:
        diff = listing["rooms"] - profile["min_rooms"]
        if diff >= 0:
            reasons.append(f"{listing['rooms']} room(s) meets minimum")
            return 1.0, reasons
        return 0.0, ["room count below minimum"]
    return 0.5, []


def _surface_score(listing: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    if not profile.get("min_surface_m2") or not listing.get("surface_m2"):
        return 0.5, []
    ratio = listing["surface_m2"] / profile["min_surface_m2"]
    if ratio >= 1.2:
        return 1.0, [f"{listing['surface_m2']} m² is generous"]
    if ratio >= 1.0:
        return 0.85, [f"{listing['surface_m2']} m² meets minimum"]
    return 0.0, [f"{listing['surface_m2']} m² is below minimum"]


def _furnished_score(listing: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    pref = profile.get("furnished_preference")
    furn = listing.get("furnished")
    if not pref or pref == "any":
        return 1.0, []
    if furn is None:
        return 0.5, ["furnished status unknown"]
    if pref == "required":
        return (1.0, ["furnished as required"]) if furn else (0.0, ["unfurnished but required"])
    if pref == "prefer":
        return (1.0, ["furnished as preferred"]) if furn else (0.5, ["unfurnished (preferred)"])
    return 0.5, []


def _room_keyword_score(listing: dict[str, Any], profile: dict[str, Any]) -> tuple[float, list[str]]:
    must_haves: list[str] = []
    nice_to_haves: list[str] = []
    rooms = profile.get("room_requirements") or {}
    for room_data in rooms.values():
        if not isinstance(room_data, dict):
            continue
        must_haves.extend(room_data.get("must_have", []) or [])
        nice_to_haves.extend(room_data.get("nice_to_have", []) or [])

    if not must_haves and not nice_to_haves:
        return 0.7, []

    text = " ".join(
        [
            (listing.get("title") or ""),
            (listing.get("description") or ""),
            (listing.get("raw_text") or ""),
        ]
    ).lower()
    features = [f.lower() for f in (listing.get("features") or [])]
    haystack = text + " " + " ".join(features)

    must_hits = [k for k in must_haves if k.lower() in haystack]
    nice_hits = [k for k in nice_to_haves if k.lower() in haystack]

    score = 0.0
    if must_haves:
        score += 0.7 * (len(must_hits) / len(must_haves))
    else:
        score += 0.7
    if nice_to_haves:
        score += 0.3 * (len(nice_hits) / len(nice_to_haves))
    else:
        score += 0.3

    reasons: list[str] = []
    if must_hits:
        reasons.append(f"matches: {', '.join(must_hits)}")
    return min(1.0, score), reasons


def _completeness_score(listing: dict[str, Any]) -> tuple[float, list[str]]:
    missing = listing.get("missing_fields") or []
    if not missing:
        return 1.0, []
    if len(missing) >= 4:
        return 0.3, [f"listing missing {len(missing)} fields"]
    return 0.6, []


def _source_score(listing: dict[str, Any]) -> tuple[float, list[str]]:
    if listing.get("is_mock"):
        return 0.5, []
    src = (listing.get("source") or "").lower()
    trusted = (
        "pap.fr",
        "seloger",
        "bienici",
        "leboncoin",
        "lodgis",
        "immobilienscout24",
        "immowelt",
        "kleinanzeigen",
        "wg-gesucht",
        "wunderflats",
        "housinganywhere",
        "spotahome",
    )
    if any(t in src for t in trusted):
        return 1.0, [f"trusted source: {listing.get('source')}"]
    return 0.7, []


def _check_commute_hard_filter(
    commute: dict[str, Any], profile: dict[str, Any]
) -> tuple[bool, list[str], list[str]]:
    """When commute is verified, enforce the (metro/bike/walk) ≤ max rule.

    Returns (passes, fail_reasons, ok_reasons).
    """
    if commute.get("status") != "verified":
        return True, [], []

    max_min = profile.get("commute_max_minutes")
    if not max_min:
        return True, [], []

    modes = profile.get("commute_modes") or ["metro", "bike"]
    metro = commute.get("metro_min")
    bike = commute.get("bike_min")
    walk = commute.get("walk_min")

    candidates: list[tuple[str, int | None]] = []
    if "metro" in modes:
        candidates.append(("Metro", metro))
    if "bike" in modes:
        candidates.append(("bike", bike))
    if "walk" in modes:
        candidates.append(("walk", walk))

    ok_reasons = [
        f"{label}: {val} min"
        for label, val in candidates
        if val is not None and val <= max_min
    ]
    if ok_reasons:
        return True, [], ok_reasons

    # Every allowed mode is over the limit (or unknown for that mode).
    fail_bits = []
    for label, val in candidates:
        if val is None:
            continue
        fail_bits.append(f"{label} {val} min > {max_min} min")
    if fail_bits:
        return False, [f"commute exceeds {max_min} min: " + "; ".join(fail_bits)], []
    return True, [], []


def score_listing(
    listing: dict[str, Any],
    profile: Any,
    *,
    commute: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Score one listing for one profile. Returns the ListingMatch fields."""
    p = _profile_dict(profile)
    if commute is None:
        commute = {
            "metro_min": None,
            "bike_min": None,
            "walk_min": None,
            "status": "unknown",
        }

    passes, hard_fail_reasons = _check_hard_filters(listing, p)
    commute_pass, commute_fail, commute_ok = _check_commute_hard_filter(commute, p)
    if not commute_pass:
        passes = False
        hard_fail_reasons = hard_fail_reasons + commute_fail

    parts: list[tuple[float, float, list[str]]] = []
    weights = {
        "budget": 0.25,
        "rooms": 0.20,
        "surface": 0.15,
        "furnished": 0.10,
        "keywords": 0.15,
        "completeness": 0.10,
        "source": 0.05,
    }

    s, r = _budget_fit_score(listing, p)
    parts.append((s, weights["budget"], r))

    s, r = _bedroom_room_score(listing, p)
    parts.append((s, weights["rooms"], r))

    s, r = _surface_score(listing, p)
    parts.append((s, weights["surface"], r))

    s, r = _furnished_score(listing, p)
    parts.append((s, weights["furnished"], r))

    s, r = _room_keyword_score(listing, p)
    parts.append((s, weights["keywords"], r))

    s, r = _completeness_score(listing)
    parts.append((s, weights["completeness"], r))

    s, r = _source_score(listing)
    parts.append((s, weights["source"], r))

    overall = sum(s * w for s, w, _ in parts)

    warnings: list[str] = []
    if commute.get("status") != "verified":
        warnings.append("Commute needs verification.")
        # Mild penalty for unknown commute (max 5 points off, never enough to reject)
        overall = overall * 0.95
    if (listing.get("missing_fields") or []):
        warnings.append("Some listing fields are missing — verify before contacting.")

    overall_int = max(0, min(100, int(round(overall * 100))))

    reasons: list[str] = []
    for _, _, r in parts:
        reasons.extend(r)
    reasons.extend(commute_ok)
    if not passes:
        reasons = hard_fail_reasons + reasons

    pref = p.get("preferred_arrondissements") or []
    if listing.get("arrondissement") in pref:
        reasons.append(f"in preferred arrondissement {listing['arrondissement']}")
        overall_int = min(100, overall_int + 3)

    return {
        "overall_score": overall_int,
        "passes_hard_filters": passes,
        "reasons": reasons[:8],
        "warnings": warnings,
        "commute": {
            "metro_min": commute.get("metro_min"),
            "bike_min": commute.get("bike_min"),
            "walk_min": commute.get("walk_min"),
            "status": commute.get("status", "unknown"),
        },
    }
