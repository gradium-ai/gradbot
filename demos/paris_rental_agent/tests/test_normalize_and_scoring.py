from __future__ import annotations

import asyncio

from src.services.normalize import english_listing_title, normalize_result
from src.services.scoring import score_listing
from src.services.tavily_search import build_queries, is_obvious_collection_url


def test_normalize_basic_french_listing():
    raw = {
        "url": "https://example.test/abc",
        "title": "2 pièces meublé Paris 11ème",
        "content": "Bel appartement de 38 m², 1 chambre, lumineux, balcon. Loyer 1450 € charges comprises. Paris 11e (75011).",
        "source": "pap.fr",
    }
    out = normalize_result(raw)
    assert out["title"] == "2 rooms furnished Paris 11ème"
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


def test_normalize_basic_german_berlin_listing():
    raw = {
        "url": "https://www.immobilienscout24.de/expose/123456789",
        "title": "Möblierte 2-Zimmer-Wohnung in Berlin Mitte",
        "content": "Helle Wohnung, 45 m², 1.400 € Warmmiete, nah am Alexanderplatz.",
        "source": "immobilienscout24.de",
    }

    out = normalize_result(raw)

    assert out["surface_m2"] == 45
    assert out["rooms"] == 2
    assert out["bedrooms"] == 1
    assert out["furnished"] is True
    assert out["total_monthly_eur"] == 1400


def test_english_listing_title_translates_common_french_search_titles():
    assert (
        english_listing_title(
            "Appartements entre particuliers à louer Paris 4ème arrondissement ..."
        )
        == "Apartments for rent by owner in Paris 4th arrondissement ..."
    )
    assert (
        english_listing_title(
            "Appartements à louer Paris 15ème arrondissement 75015 , Seloger.com"
        )
        == "Apartments for rent in Paris 15th arrondissement 75015, Seloger.com"
    )
    assert (
        english_listing_title("Location immobilière Paris (75) - Bien'ici")
        == "Rental property Paris (75) - Bien'ici"
    )


def test_tavily_filters_obvious_collection_urls():
    assert is_obvious_collection_url(
        "https://www.pap.fr/annonce/locations-appartement-paris-75-g439"
    )
    assert is_obvious_collection_url(
        "https://www.pap.fr/annonce/recherche-location-appartement-paris-75-g439-10"
    )
    assert is_obvious_collection_url(
        "https://www.seloger.com/recherche/location/appartement/paris-75000/paris-12eme-arrondissement-75012/ad09fr37"
    )
    assert is_obvious_collection_url(
        "https://www.bienici.com/recherche/location/paris-75000/appartement"
    )
    assert is_obvious_collection_url(
        "https://www.lodgis.com/fr/paris,location-meublee/location-1-chambre-meuble-paris_15565.cat.html"
    )
    assert not is_obvious_collection_url(
        "https://www.pap.fr/annonces/appartement-paris-11e-r123456789"
    )
    assert not is_obvious_collection_url(
        "https://www.bienici.com/annonce/location/paris-11e/appartement/2pieces/foo"
    )


def test_tavily_filters_berlin_collection_urls_and_keeps_detail_urls():
    assert is_obvious_collection_url(
        "https://www.spotahome.com/de/s/berlin/for-rent%3Aapartments"
    )
    assert is_obvious_collection_url(
        "https://www.immobilienscout24.de/Suche/de/berlin/berlin/wohnung-mieten"
    )
    assert is_obvious_collection_url(
        "https://www.wunderflats.com/en/furnished-apartments/berlin"
    )
    assert is_obvious_collection_url(
        "https://housinganywhere.com/s/Berlin--Germany/apartment-for-rent"
    )

    assert not is_obvious_collection_url(
        "https://www.spotahome.com/berlin/for-rent%3Aapartments/608504"
    )
    assert not is_obvious_collection_url(
        "https://www.immobilienscout24.de/expose/123456789"
    )
    assert not is_obvious_collection_url(
        "https://www.immowelt.de/expose/2abcde"
    )
    assert not is_obvious_collection_url(
        "https://www.wunderflats.com/en/furnished-apartment/bright-flat-berlin"
    )
    assert not is_obvious_collection_url(
        "https://housinganywhere.com/room/1234567/de/Berlin/foo"
    )


def test_tavily_queries_target_detail_listing_paths():
    queries = build_queries({"min_bedrooms": 1, "max_rent_including_charges_eur": 1500})

    assert any("site:pap.fr/annonces" in query for query in queries)
    assert any("site:seloger.com/annonces/locations/appartement" in query for query in queries)
    assert any("site:bienici.com/annonce/location" in query for query in queries)


def test_tavily_queries_support_berlin_market():
    queries = build_queries({
        "city": "berlin",
        "min_bedrooms": 1,
        "max_rent_including_charges_eur": 1500,
        "furnished_preference": "required",
    })

    joined = " ".join(queries)
    assert "Berlin" in joined
    assert "Wohnung" in joined
    assert any("site:immobilienscout24.de/expose" in query for query in queries)
    assert any("site:immowelt.de/expose" in query for query in queries)
    assert any("site:spotahome.com/berlin/for-rent:apartments" in query for query in queries)


def test_commute_cache_is_scoped_by_city(monkeypatch):
    from src.services import commute

    calls = []
    commute._CACHE.clear()
    monkeypatch.setattr(commute, "_resolve_google_maps_key", lambda: "test-key")

    async def fake_call(_client, _api_key, _origin, destinations, mode, *, city):
        calls.append((city, mode, tuple(destinations)))
        duration = 10 if city == "paris" else 20
        return [{"duration_minutes": duration, "status": "ok"} for _ in destinations]

    monkeypatch.setattr(commute, "_call_routes_matrix", fake_call)

    paris = asyncio.run(
        commute.compute_commute_batch("Central", ["Center"], city="paris")
    )
    berlin = asyncio.run(
        commute.compute_commute_batch("Central", ["Center"], city="berlin")
    )

    assert paris["Center"]["metro_min"] == 10
    assert berlin["Center"]["metro_min"] == 20
    assert {call[0] for call in calls} == {"paris", "berlin"}


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
