"""Extract apartment-search requirements from a free-form transcript.

Deterministic regex/keyword extraction. Works without an LLM key. Designed to be
called from REST endpoints, the chat assistant, and the Gradbot voice tool.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Optional

REQUIRED_FIELDS = [
    "work_location_address_or_label",
    "max_rent_including_charges_eur",
    "min_bedrooms_or_rooms_or_surface",
    "commute_max_minutes",
    "commute_modes",
]

# Keyword tables (lowercase, accents stripped for matching)
ROOM_FEATURE_KEYWORDS = {
    "natural light": ("living_room", "must_have", "natural light"),
    "good light": ("living_room", "must_have", "natural light"),
    "lots of light": ("living_room", "must_have", "natural light"),
    "lumineux": ("living_room", "must_have", "natural light"),
    "desk": ("living_room", "must_have", "desk space"),
    "desk space": ("living_room", "must_have", "desk space"),
    "space for a desk": ("living_room", "must_have", "desk space"),
    "bureau": ("living_room", "must_have", "desk space"),
    "proper kitchen": ("kitchen", "must_have", "proper kitchen"),
    "real kitchen": ("kitchen", "must_have", "proper kitchen"),
    "open kitchen": ("kitchen", "nice_to_have", "open kitchen"),
    "cuisine ouverte": ("kitchen", "nice_to_have", "open kitchen"),
    "oven": ("kitchen", "must_have", "oven"),
    "four": ("kitchen", "must_have", "oven"),
    "dishwasher": ("kitchen", "nice_to_have", "dishwasher"),
    "lave-vaisselle": ("kitchen", "nice_to_have", "dishwasher"),
    "balcony": ("living_room", "nice_to_have", "balcony"),
    "balcon": ("living_room", "nice_to_have", "balcony"),
    "storage": ("living_room", "nice_to_have", "storage"),
    "rangement": ("living_room", "nice_to_have", "storage"),
    "double bed": ("bedroom", "must_have", "double bed"),
    "lit double": ("bedroom", "must_have", "double bed"),
    "wardrobe": ("bedroom", "nice_to_have", "wardrobe"),
    "armoire": ("bedroom", "nice_to_have", "wardrobe"),
    "sofa": ("living_room", "nice_to_have", "sofa"),
    "canape": ("living_room", "nice_to_have", "sofa"),
    "dining table": ("living_room", "nice_to_have", "dining table"),
}

NEARBY_KEYWORDS = {
    "supermarket": ("supermarket_m", 500),
    "supermarche": ("supermarket_m", 500),
    "metro": ("metro_m", 700),
    "metro station": ("metro_m", 700),
    "subway": ("metro_m", 700),
    "hospital": ("hospital_m", 2000),
    "hopital": ("hospital_m", 2000),
    "park": ("park_m", 1000),
    "parc": ("park_m", 1000),
    "gym": ("gym_m", 1500),
    "school": ("school_m", 1500),
    "ecole": ("school_m", 1500),
    "pharmacy": ("pharmacy_m", 700),
    "pharmacie": ("pharmacy_m", 700),
    "bakery": ("bakery_m", 500),
    "boulangerie": ("bakery_m", 500),
}

ARRONDISSEMENT_PATTERNS = [
    re.compile(r"paris\s+(\d{1,2})(?:e|ème|st|nd|rd|th)?\b", re.IGNORECASE),
    re.compile(r"\b(\d{1,2})(?:e|ème)\s+arrondissement\b", re.IGNORECASE),
    re.compile(r"\b750(\d{2})\b"),
]


@dataclass
class ExtractedRequirements:
    profile_patch: dict[str, Any] = field(default_factory=dict)
    summary: str = ""
    missing_fields: list[str] = field(default_factory=list)
    ambiguous_fields: list[str] = field(default_factory=list)
    field_confidence: dict[str, float] = field(default_factory=dict)
    field_sources: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "profile_patch": self.profile_patch,
            "summary": self.summary,
            "missing_fields": self.missing_fields,
            "ambiguous_fields": self.ambiguous_fields,
            "field_confidence": self.field_confidence,
            "field_sources": self.field_sources,
        }


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def _normalize(text: str) -> str:
    return _strip_accents(text.lower())


def _extract_budget(text: str) -> tuple[Optional[int], float]:
    """Look for a max rent number in EUR. Returns (value, confidence)."""
    norm = text
    candidates: list[tuple[int, float]] = []

    # Match "max 1500", "under 1600", "not more than 1700", "budget 1400"
    patterns: list[tuple[str, float]] = [
        (r"(?:max(?:imum)?|up to|under|less than|not more than|budget(?: is)?|au max(?:imum)?|max\.|jusqu'à|jusqu'a|maximum de)\s*(?:de\s*)?(?:€|eur(?:os)?)?\s*(\d{3,5})\s*(?:€|eur(?:os)?|euros)?", 0.95),
        (r"(\d{3,5})\s*(?:€|eur|euros)\s*(?:max|maximum)?\s*(?:par mois|/mois|monthly|per month|including charges|charges? comprises?|cc|tcc|c\.c\.)?", 0.85),
        (r"around\s+(\d{3,5})\s*(?:€|eur|euros)?", 0.7),
    ]
    for pattern, conf in patterns:
        for m in re.finditer(pattern, norm, flags=re.IGNORECASE):
            try:
                val = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 200 <= val <= 20000:
                candidates.append((val, conf))

    if not candidates:
        return None, 0.0
    # Prefer highest-confidence, then smallest (people say "max")
    candidates.sort(key=lambda x: (-x[1], x[0]))
    return candidates[0]


def _extract_rooms_and_bedrooms(text: str) -> dict[str, Any]:
    norm = _normalize(text)
    out: dict[str, Any] = {}

    if re.search(r"\bstudio\b", norm):
        out["rooms"] = 1
        out["bedrooms"] = 0
        out["confidence"] = 0.95
        return out

    # Words: one-bedroom, two bedrooms, etc.
    word_to_n = {
        "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
        "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
    }

    m = re.search(r"\b(\d+|one|two|three|four|five|un|une|deux|trois|quatre|cinq)[\s-]+bedroom", norm)
    if m:
        token = m.group(1)
        n = int(token) if token.isdigit() else word_to_n.get(token)
        if n is not None:
            out["bedrooms"] = n
            out["confidence"] = 0.9

    m = re.search(r"\b(\d+|deux|trois|quatre|cinq)\s*(?:pieces?|chambre)", norm)
    if m:
        token = m.group(1)
        n = int(token) if token.isdigit() else word_to_n.get(token)
        if n is not None and "chambre" in m.group(0):
            out["bedrooms"] = n
            out["confidence"] = max(out.get("confidence", 0), 0.9)
        elif n is not None:
            out["rooms"] = n
            out["confidence"] = max(out.get("confidence", 0), 0.85)

    m = re.search(r"\bt(\d)\b", norm)
    if m:
        n = int(m.group(1))
        out["rooms"] = n
        out["bedrooms"] = max(0, n - 1)
        out["confidence"] = max(out.get("confidence", 0), 0.9)

    return out


def _extract_surface(text: str) -> tuple[Optional[int], float]:
    norm = _normalize(text)
    patterns = [
        (r"(?:at least|minimum|min|au moins|au minimum)\s*(\d{2,3})\s*(?:m2|m²|sqm|square meters)", 0.9),
        (r"(\d{2,3})\s*(?:m2|m²|sqm|square meters)", 0.7),
    ]
    for pattern, conf in patterns:
        m = re.search(pattern, norm)
        if m:
            try:
                val = int(m.group(1))
                if 8 <= val <= 500:
                    return val, conf
            except ValueError:
                continue
    return None, 0.0


def _extract_furnished(text: str) -> tuple[Optional[str], float]:
    norm = _normalize(text)
    if re.search(r"\b(unfurnished|non meuble|non-meuble|empty|vide)\b", norm):
        return "any", 0.85  # explicitly not required, but tolerable
    if re.search(r"\b(furnished|meuble)\b", norm):
        if "prefer" in norm or "ideally" in norm or "would like" in norm:
            return "prefer", 0.85
        return "required", 0.9
    return None, 0.0


def _extract_commute(text: str) -> dict[str, Any]:
    norm = _normalize(text)
    out: dict[str, Any] = {}

    # commute_max_minutes
    m = re.search(r"(\d{1,3})\s*(?:min(?:utes)?|mn)\b", norm)
    if m:
        try:
            val = int(m.group(1))
            if 5 <= val <= 240:
                out["commute_max_minutes"] = val
                out["minutes_confidence"] = 0.9
        except ValueError:
            pass
    if "half an hour" in norm or "half hour" in norm:
        out.setdefault("commute_max_minutes", 30)
        out["minutes_confidence"] = max(out.get("minutes_confidence", 0), 0.85)

    modes: list[str] = []
    if re.search(r"\b(metro|subway)\b", norm):
        modes.append("metro")
    if re.search(r"\bbike|bicycle|velo|cycling\b", norm):
        modes.append("bike")
    if re.search(r"\bwalk(ing)?|a pied\b", norm):
        modes.append("walk")
    if re.search(r"\bbus\b", norm):
        modes.append("bus")
    if modes:
        out["commute_modes"] = sorted(set(modes))
        out["modes_confidence"] = 0.9

    if "metro or bike" in norm or "metro ou velo" in norm:
        out["commute_logic"] = "metro_or_bike"

    return out


def _extract_work_location(text: str) -> dict[str, Any]:
    """Try to locate a workplace mention. Conservative: only label, not address."""
    norm_orig = text
    out: dict[str, Any] = {}

    patterns = [
        r"(?:my office is|i work)\s+(?:near|at|by|close to)\s+([A-Za-zÀ-ÿ0-9' \-]+?)(?:[\.,;]|$|\s+(?:and|in)\b)",
        r"(?:office|workplace|work)\s+(?:is\s+)?(?:near|at|by|close to)\s+([A-Za-zÀ-ÿ0-9' \-]+?)(?:[\.,;]|$|\s+(?:and|in)\b)",
        r"(?:near|close to)\s+(?:my\s+)?(?:office|workplace|work)\s+(?:near|at|by)\s+([A-Za-zÀ-ÿ0-9' \-]+?)(?:[\.,;]|$)",
        r"my\s+work\s+is\s+(?:at\s+)?([A-Za-zÀ-ÿ0-9' \-]+?)(?:[\.,;]|$)",
    ]
    for pattern in patterns:
        m = re.search(pattern, norm_orig, flags=re.IGNORECASE)
        if m:
            label = m.group(1).strip(" .,")
            if 2 <= len(label) <= 80:
                out["work_location_label"] = label
                out["confidence"] = 0.7
                break
    return out


def _extract_room_features(text: str) -> dict[str, Any]:
    norm = _normalize(text)
    explicit_must_have = bool(
        re.search(
            r"\b("
            r"must[ -]?haves?|required|need|needs|require|requires|"
            r"want|wants|want to have|really like to have|would really like|"
            r"would like to have|i'd like to have|id like to have"
            r")\b",
            norm,
        )
    )
    rooms: dict[str, dict[str, list[str]]] = {
        "living_room": {"must_have": [], "nice_to_have": []},
        "bedroom": {"must_have": [], "nice_to_have": []},
        "kitchen": {"must_have": [], "nice_to_have": []},
    }
    found = False
    for kw, (room, kind, label) in ROOM_FEATURE_KEYWORDS.items():
        if kw in norm:
            target_kind = "must_have" if explicit_must_have else kind
            if label not in rooms[room][target_kind]:
                rooms[room][target_kind].append(label)
                found = True
    return rooms if found else {}


def _extract_nearby(text: str) -> dict[str, Any]:
    norm = _normalize(text)
    out: dict[str, int] = {}
    for kw, (key, default_m) in NEARBY_KEYWORDS.items():
        if kw in norm:
            out[key] = default_m
    return out


def _extract_arrondissements(text: str) -> dict[str, list[int]]:
    out_pref: list[int] = []
    out_excl: list[int] = []
    norm = text.lower()
    excluded_zone = "not " in norm or "avoid" in norm or "except" in norm
    for pat in ARRONDISSEMENT_PATTERNS:
        for m in pat.finditer(norm):
            try:
                arr = int(m.group(1))
                if 1 <= arr <= 20 and arr not in out_pref:
                    if excluded_zone:
                        out_excl.append(arr)
                    else:
                        out_pref.append(arr)
            except ValueError:
                continue
    result: dict[str, list[int]] = {}
    if out_pref:
        result["preferred_arrondissements"] = out_pref
    if out_excl:
        result["excluded_arrondissements"] = out_excl
    return result


def _build_summary(patch: dict[str, Any]) -> str:
    parts: list[str] = []
    if patch.get("furnished_preference") == "required":
        parts.append("furnished")
    if patch.get("min_bedrooms") is not None:
        if patch["min_bedrooms"] == 0:
            parts.append("studio")
        else:
            parts.append(f"{patch['min_bedrooms']}-bedroom")
    elif patch.get("min_rooms") is not None:
        parts.append(f"{patch['min_rooms']}-room")
    if patch.get("min_surface_m2"):
        parts.append(f"min {patch['min_surface_m2']} m²")

    head = " ".join(parts) or "apartment"

    bits: list[str] = [f"You want a {head} in Paris"]
    if patch.get("max_rent_including_charges_eur"):
        bits.append(f"max €{patch['max_rent_including_charges_eur']} including charges")

    work = patch.get("work_location_label") or patch.get("work_location_address")
    if work and patch.get("commute_max_minutes"):
        modes = " or ".join(patch.get("commute_modes") or ["metro", "bike"])
        bits.append(f"within {patch['commute_max_minutes']} min of {work} by {modes}")
    elif work:
        bits.append(f"near {work}")

    rooms = patch.get("room_requirements") or {}
    feature_words: list[str] = []
    for room_key in ("living_room", "kitchen", "bedroom"):
        feats = rooms.get(room_key, {}).get("must_have", [])
        feature_words.extend(feats)
    feature_words.extend(rooms.get("kitchen", {}).get("nice_to_have", []))
    if feature_words:
        bits.append("with " + ", ".join(dict.fromkeys(feature_words)))

    nearby = patch.get("nearby_requirements") or {}
    nearby_human = []
    if "supermarket_m" in nearby:
        nearby_human.append("a supermarket")
    if "metro_m" in nearby:
        nearby_human.append("Metro")
    if "park_m" in nearby:
        nearby_human.append("a park")
    if nearby_human:
        bits.append(f"with {' and '.join(nearby_human)} nearby")

    text = ". ".join(bits) + "."
    text += " Please review the profile and correct anything that is wrong."
    return text


def _compute_missing(patch: dict[str, Any], existing: Optional[dict[str, Any]] = None) -> list[str]:
    """Return human-readable required-field keys that are still missing."""
    merged = dict(existing or {})
    merged.update({k: v for k, v in patch.items() if v is not None and v != [] and v != {}})

    missing: list[str] = []
    if not (merged.get("work_location_address") or merged.get("work_location_label")):
        missing.append("work_location")
    if merged.get("max_rent_including_charges_eur") in (None, 0):
        missing.append("max_rent_including_charges_eur")
    has_size = any(
        merged.get(k) not in (None, 0)
        for k in ("min_bedrooms", "min_rooms", "min_surface_m2")
    )
    if not has_size:
        missing.append("min_bedrooms_or_rooms_or_surface")
    if not merged.get("commute_max_minutes"):
        missing.append("commute_max_minutes")
    if not (merged.get("commute_modes") or []):
        missing.append("commute_modes")
    return missing


def extract_requirements(
    transcript: str,
    *,
    existing_profile: Optional[dict[str, Any]] = None,
) -> ExtractedRequirements:
    """Deterministic, regex-driven extraction. Never invents values."""
    if not transcript or not transcript.strip():
        return ExtractedRequirements(
            summary="",
            missing_fields=_compute_missing({}, existing_profile),
        )

    patch: dict[str, Any] = {}
    confidence: dict[str, float] = {}
    sources: dict[str, str] = {}
    ambiguous: list[str] = []

    # Budget
    budget, conf = _extract_budget(transcript)
    if budget is not None:
        patch["max_rent_including_charges_eur"] = budget
        confidence["max_rent_including_charges_eur"] = conf
        sources["max_rent_including_charges_eur"] = "voice"

    # Rooms / bedrooms
    rb = _extract_rooms_and_bedrooms(transcript)
    if "bedrooms" in rb:
        patch["min_bedrooms"] = rb["bedrooms"]
        confidence["min_bedrooms"] = rb.get("confidence", 0.85)
        sources["min_bedrooms"] = "voice"
    if "rooms" in rb:
        patch["min_rooms"] = rb["rooms"]
        confidence["min_rooms"] = rb.get("confidence", 0.85)
        sources["min_rooms"] = "voice"

    # Surface
    surf, conf = _extract_surface(transcript)
    if surf is not None:
        patch["min_surface_m2"] = surf
        confidence["min_surface_m2"] = conf
        sources["min_surface_m2"] = "voice"

    # Furnished
    furn, conf = _extract_furnished(transcript)
    if furn is not None:
        patch["furnished_preference"] = furn
        confidence["furnished_preference"] = conf
        sources["furnished_preference"] = "voice"

    # Commute
    commute = _extract_commute(transcript)
    if "commute_max_minutes" in commute:
        patch["commute_max_minutes"] = commute["commute_max_minutes"]
        confidence["commute_max_minutes"] = commute.get("minutes_confidence", 0.85)
        sources["commute_max_minutes"] = "voice"
    if "commute_modes" in commute:
        patch["commute_modes"] = commute["commute_modes"]
        confidence["commute_modes"] = commute.get("modes_confidence", 0.85)
        sources["commute_modes"] = "voice"
    if "commute_logic" in commute:
        patch["commute_logic"] = commute["commute_logic"]
        sources["commute_logic"] = "voice"

    # Work location
    work = _extract_work_location(transcript)
    if "work_location_label" in work:
        patch["work_location_label"] = work["work_location_label"]
        confidence["work_location_label"] = work.get("confidence", 0.7)
        sources["work_location_label"] = "voice"
        if work.get("confidence", 0.7) < 0.75:
            ambiguous.append("work_location_label")

    # Room features
    rooms = _extract_room_features(transcript)
    if rooms:
        patch["room_requirements"] = rooms
        sources["room_requirements"] = "voice"

    # Nearby
    nearby = _extract_nearby(transcript)
    if nearby:
        patch["nearby_requirements"] = nearby
        sources["nearby_requirements"] = "voice"

    # Arrondissements
    arr = _extract_arrondissements(transcript)
    if arr:
        patch.update(arr)
        for k in arr:
            sources[k] = "voice"

    summary = _build_summary({**(existing_profile or {}), **patch}) if patch else ""
    missing = _compute_missing(patch, existing_profile)

    return ExtractedRequirements(
        profile_patch=patch,
        summary=summary,
        missing_fields=missing,
        ambiguous_fields=ambiguous,
        field_confidence=confidence,
        field_sources=sources,
    )


def compute_missing_fields(profile: dict[str, Any]) -> list[str]:
    """Public helper used by REST endpoints to compute missing required fields."""
    return _compute_missing({}, profile)
