from __future__ import annotations

from app.services.normalize import normalize_result
from app.services.scoring import score_listing


def test_normalize_basic_french_listing():
    raw = {
        "url": "https://example.test/abc",
        "title": "2 pièces meublé Paris 11ème",
        "content": "Bel appartement de 38 m², 1 chambre, lumineux, balcon. Loyer 1450 € charges comprises. Paris 11e (75011).",
        "source": "pap.fr",
    }
    out = normalize_result(raw)
    assert out["surface_m2"] == 38
    assert out["bedrooms"] == 1
    assert out["arrondissement"] == 11
    assert out["furnished"] is True
    assert out["total_monthly_eur"] == 1450
    assert "url_hash" in out and len(out["url_hash"]) <= 32


def test_normalize_handles_missing_data():
    raw = {"url": "", "title": "Studio", "content": ""}
    out = normalize_result(raw)
    assert out["title"] == "Studio"
    assert "rent_eur" in out["missing_fields"]


def test_score_strong_match_passes_filters():
    listing = {
        "title": "Furnished 2-room Paris 11",
        "description": "Furnished 38 m² 1 bedroom, balcony, supermarket, metro",
        "rent_eur": 1400,
        "total_monthly_eur": 1400,
        "surface_m2": 38,
        "rooms": 2,
        "bedrooms": 1,
        "furnished": True,
        "arrondissement": 11,
        "features": ["balcony"],
        "is_mock": True,
    }
    profile = {
        "max_rent_including_charges_eur": 1500,
        "min_bedrooms": 1,
        "min_surface_m2": 30,
        "furnished_preference": "required",
        "preferred_arrondissements": [11],
        "room_requirements": {
            "living_room": {"must_have": ["natural light"], "nice_to_have": []},
            "kitchen": {"must_have": [], "nice_to_have": []},
            "bedroom": {"must_have": [], "nice_to_have": []},
        },
        "nearby_requirements": {"supermarket_m": 500, "metro_m": 700},
    }
    out = score_listing(listing, profile)
    assert out["passes_hard_filters"] is True
    assert out["overall_score"] >= 50
    assert out["commute"]["status"] == "unknown"


def test_score_over_budget_fails_hard_filter():
    listing = {
        "rent_eur": 2000, "total_monthly_eur": 2000, "bedrooms": 1, "surface_m2": 40,
        "furnished": True, "arrondissement": 6,
    }
    profile = {
        "max_rent_including_charges_eur": 1500, "min_bedrooms": 1, "min_surface_m2": 30,
        "furnished_preference": "required",
    }
    out = score_listing(listing, profile)
    assert out["passes_hard_filters"] is False
    assert any("budget" in r.lower() or "exceeds" in r.lower() for r in out["reasons"])


def test_score_too_small_fails():
    listing = {"surface_m2": 14, "bedrooms": 0, "rooms": 1, "rent_eur": 800, "total_monthly_eur": 800}
    profile = {"max_rent_including_charges_eur": 1500, "min_surface_m2": 25}
    out = score_listing(listing, profile)
    assert out["passes_hard_filters"] is False


def test_score_verified_commute_within_limit_passes():
    listing = {
        "rent_eur": 1400, "total_monthly_eur": 1400, "bedrooms": 1, "surface_m2": 38,
        "furnished": True, "arrondissement": 11,
    }
    profile = {
        "max_rent_including_charges_eur": 1500, "min_bedrooms": 1, "min_surface_m2": 30,
        "commute_max_minutes": 30, "commute_modes": ["metro", "bike"],
    }
    commute = {"metro_min": 22, "bike_min": 18, "walk_min": 55, "status": "verified"}
    out = score_listing(listing, profile, commute=commute)
    assert out["passes_hard_filters"] is True
    assert out["commute"]["status"] == "verified"
    assert out["commute"]["metro_min"] == 22
    assert "Commute needs verification." not in out["warnings"]


def test_score_verified_commute_too_far_fails():
    listing = {
        "rent_eur": 1400, "total_monthly_eur": 1400, "bedrooms": 1, "surface_m2": 38,
        "furnished": True, "arrondissement": 18,
    }
    profile = {
        "max_rent_including_charges_eur": 1500, "min_bedrooms": 1, "min_surface_m2": 30,
        "commute_max_minutes": 30, "commute_modes": ["metro", "bike"],
    }
    commute = {"metro_min": 55, "bike_min": 48, "walk_min": 95, "status": "verified"}
    out = score_listing(listing, profile, commute=commute)
    assert out["passes_hard_filters"] is False
    assert any("commute exceeds" in r.lower() for r in out["reasons"])
