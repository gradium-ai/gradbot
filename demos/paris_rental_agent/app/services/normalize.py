"""Normalize raw search results into Listing-compatible dicts. Conservative parsing only."""

from __future__ import annotations

import hashlib
import re
import unicodedata
from typing import Any, Optional
from urllib.parse import urlparse


def _strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def url_hash(url: str) -> str:
    return hashlib.sha256((url or "").encode("utf-8")).hexdigest()[:32]


def safe_external_url(url: str) -> Optional[str]:
    """Return a browser-safe external URL, or None for unsafe/invalid schemes."""
    url = (url or "").strip()
    if not url:
        return None
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _parse_int_eur(s: str) -> Optional[int]:
    m = re.search(r"(\d{1,2}(?:[\s \.]?\d{3})?)", s.replace(",", "."))
    if not m:
        return None
    cleaned = re.sub(r"[\s \.]", "", m.group(1))
    try:
        v = int(cleaned)
        return v if 50 <= v <= 50000 else None
    except ValueError:
        return None


def parse_rent(text: str) -> tuple[Optional[int], Optional[int], Optional[int]]:
    """Returns (rent_eur, charges_eur, total_monthly_eur)."""
    rent: Optional[int] = None
    charges: Optional[int] = None

    # "1 450 €" / "1450€" / "1450 euros" / "1450 / mois"
    candidates = []
    for m in re.finditer(
        r"(\d{1,2}[\s \.]?\d{3}|\d{3,4})\s*(?:€|eur(?:os)?|euros?\b)",
        text,
        flags=re.IGNORECASE,
    ):
        v = _parse_int_eur(m.group(0))
        if v is not None:
            candidates.append(v)

    if candidates:
        rent = max(candidates)  # pick the largest, often "total"

    norm = _strip_accents(text.lower())
    if rent is None:
        m = re.search(r"loyer[^\d]{0,20}(\d{3,5})", norm)
        if m:
            try:
                v = int(m.group(1))
                rent = v if 200 <= v <= 50000 else rent
            except ValueError:
                pass

    if "charges comprises" in norm or "cc" in norm or "tcc" in norm or "c.c." in norm:
        # rent already includes charges
        return None, None, rent

    m = re.search(r"charges?[^\d]{0,20}(\d{2,4})", norm)
    if m:
        try:
            charges = int(m.group(1))
        except ValueError:
            pass

    total = None
    if rent is not None:
        total = rent + (charges or 0)
    return rent, charges, total


def parse_surface(text: str) -> Optional[int]:
    norm = _strip_accents(text.lower())
    m = re.search(r"(\d{1,3})\s*(?:m2|m²|sqm)", norm)
    if m:
        try:
            v = int(m.group(1))
            if 8 <= v <= 500:
                return v
        except ValueError:
            return None
    return None


def parse_rooms(text: str) -> tuple[Optional[int], Optional[int]]:
    """Returns (rooms, bedrooms)."""
    norm = _strip_accents(text.lower())
    rooms: Optional[int] = None
    bedrooms: Optional[int] = None

    if re.search(r"\bstudio\b", norm):
        return 1, 0

    m = re.search(r"\bt(\d)\b", norm)
    if m:
        rooms = int(m.group(1))
        bedrooms = max(0, rooms - 1)

    m2 = re.search(r"(\d+)\s*pieces?", norm)
    if m2:
        try:
            rooms = int(m2.group(1))
            if bedrooms is None:
                bedrooms = max(0, rooms - 1)
        except ValueError:
            pass

    m3 = re.search(r"(\d+)\s*chambres?", norm)
    if m3:
        try:
            bedrooms = int(m3.group(1))
        except ValueError:
            pass

    if not bedrooms and re.search(r"\b(one|1)[\s-]*bedroom", norm):
        bedrooms = 1
    if not bedrooms and re.search(r"\b(two|2)[\s-]*bedroom", norm):
        bedrooms = 2

    return rooms, bedrooms


def parse_furnished(text: str) -> Optional[bool]:
    norm = _strip_accents(text.lower())
    if re.search(r"\bnon[ -]?meuble\b|\bunfurnished\b|\bempty\b|\bvide\b", norm):
        return False
    if re.search(r"\bmeuble\b|\bfurnished\b", norm):
        return True
    return None


def parse_arrondissement(text: str) -> Optional[int]:
    norm = _strip_accents(text.lower())
    m = re.search(r"\b750(\d{2})\b", norm)
    if m:
        try:
            v = int(m.group(1))
            if 1 <= v <= 20:
                return v
        except ValueError:
            pass
    m = re.search(r"paris\s+(\d{1,2})(?:e|eme|er|st|nd|rd|th)?\b", norm)
    if m:
        try:
            v = int(m.group(1))
            if 1 <= v <= 20:
                return v
        except ValueError:
            pass
    m = re.search(r"\b(\d{1,2})(?:eme|e)\s+arrondissement\b", norm)
    if m:
        try:
            v = int(m.group(1))
            if 1 <= v <= 20:
                return v
        except ValueError:
            pass
    return None


def _english_ordinal(n: int) -> str:
    suffix = "th"
    if n % 100 not in {11, 12, 13}:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def english_listing_title(title: str) -> str:
    """Translate common French rental-result titles for the English UI."""
    out = re.sub(r"\s+", " ", (title or "").strip())
    if not out:
        return "Untitled listing"

    replacements = [
        (r"\bAppartements?\s+entre\s+particuliers\s+[àa]\s+louer\b", "Apartments for rent by owner in"),
        (r"\bAppartements?\s+[àa]\s+louer\b", "Apartments for rent in"),
        (r"\bAppartement\s+meubl[ée]\b", "Furnished apartment"),
        (r"\bAppartement\b", "Apartment"),
        (r"\bLocation\s+immobili[èe]re\b", "Rental property"),
        (r"\bLocation\s+appartement\b", "Apartment rental"),
        (r"\bImmobilier\b", "Real estate"),
        (r"\bnon\s+meubl[ée]\b", "unfurnished"),
        (r"\bmeubl[ée]\b", "furnished"),
        (r"\bpi[èe]ces?\b", "rooms"),
        (r"\bchambres?\b", "bedrooms"),
        (r"\b[àa]\s+louer\b", "for rent"),
    ]
    for pattern, repl in replacements:
        out = re.sub(pattern, repl, out, flags=re.IGNORECASE)

    def _arrondissement(match: re.Match[str]) -> str:
        return f"{_english_ordinal(int(match.group(1)))} arrondissement"

    out = re.sub(
        r"\b(\d{1,2})(?:er|e|eme|ème)\s+arrondissement\b",
        _arrondissement,
        out,
        flags=re.IGNORECASE,
    )
    out = re.sub(r"\s+,", ",", out)
    out = re.sub(r"\s{2,}", " ", out).strip()
    return out or "Untitled listing"


FEATURE_KEYWORDS = [
    ("balcony", ["balcony", "balcon"]),
    ("terrace", ["terrace", "terrasse"]),
    ("elevator", ["elevator", "ascenseur"]),
    ("dishwasher", ["dishwasher", "lave-vaisselle"]),
    ("oven", ["oven", "four"]),
    ("furnished", ["furnished", "meuble"]),
    ("parking", ["parking"]),
    ("metro_nearby", ["metro"]),
    ("supermarket_nearby", ["supermarket", "supermarche"]),
]


def parse_features(text: str) -> list[str]:
    norm = _strip_accents(text.lower())
    found: list[str] = []
    for label, kws in FEATURE_KEYWORDS:
        if any(kw in norm for kw in kws):
            found.append(label)
    return found


REQUIRED_LISTING_FIELDS = [
    "rent_eur",
    "total_monthly_eur",
    "surface_m2",
    "rooms",
    "bedrooms",
    "address_text",
    "arrondissement",
    "furnished",
]


def normalize_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw search result dict into a Listing-compatible dict.

    Expected raw fields (any may be missing):
      - url / link
      - title / name
      - description / content / body / snippet
      - source
      - is_mock
    """
    url = safe_external_url(raw.get("url") or raw.get("link") or "")
    raw_title = (raw.get("title") or raw.get("name") or "Untitled listing").strip()
    title = english_listing_title(raw_title)
    desc = (
        raw.get("description")
        or raw.get("content")
        or raw.get("body")
        or raw.get("snippet")
        or ""
    ).strip()

    body = " ".join([raw_title, desc])

    rent, charges, total = parse_rent(body)
    if rent is None and "rent_eur" in raw:
        rent = raw.get("rent_eur")
    if charges is None and "charges_eur" in raw:
        charges = raw.get("charges_eur")
    if total is None:
        if rent is not None:
            total = rent + (charges or 0)
        elif "total_monthly_eur" in raw:
            total = raw.get("total_monthly_eur")

    surface = parse_surface(body) or raw.get("surface_m2")
    rooms, bedrooms = parse_rooms(body)
    if rooms is None:
        rooms = raw.get("rooms")
    if bedrooms is None:
        bedrooms = raw.get("bedrooms")
    furnished = parse_furnished(body)
    if furnished is None and "furnished" in raw:
        furnished = raw.get("furnished")
    arrondissement = parse_arrondissement(body) or raw.get("arrondissement")
    features = parse_features(body)
    if "features" in raw:
        for f in raw["features"]:
            if f not in features:
                features.append(f)

    address_text = raw.get("address_text") or raw.get("address")
    if not address_text and arrondissement:
        address_text = f"Paris {arrondissement:02d}"

    out = {
        "canonical_url": url,
        "url_hash": url_hash(url or title),
        "source": raw.get("source"),
        "title": title,
        "description": desc or None,
        "rent_eur": rent,
        "charges_eur": charges,
        "total_monthly_eur": total,
        "surface_m2": surface,
        "rooms": rooms,
        "bedrooms": bedrooms,
        "furnished": furnished,
        "address_text": address_text,
        "arrondissement": arrondissement,
        "features": features,
        "raw_text": body[:5000] if body else None,
        "raw_data": raw,
        "is_mock": bool(raw.get("is_mock")),
    }
    out["missing_fields"] = [f for f in REQUIRED_LISTING_FIELDS if out.get(f) in (None, [], "")]
    return out
