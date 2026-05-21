"""Tests for requirement extraction."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from src.services import requirement_extraction
from src.services.requirement_extraction import (
    extract_requirements,
    extract_requirements_with_llm,
    compute_missing_fields,
)


SAMPLE = (
    "I'm looking for a furnished one-bedroom in Paris, max 1500 euros including charges, "
    "not more than 30 minutes from my office near République by metro or bike. "
    "I want good light, a proper kitchen, space for a desk, and I'd like a supermarket and metro nearby."
)


def test_extracts_budget_and_bedrooms():
    result = extract_requirements(SAMPLE)
    p = result.profile_patch
    assert p.get("max_rent_including_charges_eur") == 1500
    assert p.get("min_bedrooms") == 1
    assert p.get("furnished_preference") == "required"


def test_extracts_commute():
    result = extract_requirements(SAMPLE)
    p = result.profile_patch
    assert p.get("commute_max_minutes") == 30
    modes = p.get("commute_modes") or []
    assert "metro" in modes and "bike" in modes


def test_extracts_work_location_label():
    result = extract_requirements(SAMPLE)
    p = result.profile_patch
    label = p.get("work_location_label")
    assert label and "République" in label


def test_extracts_room_features_and_amenities():
    result = extract_requirements(SAMPLE)
    p = result.profile_patch
    rooms = p.get("room_requirements") or {}
    must_have_living = rooms.get("living_room", {}).get("must_have", [])
    must_have_kitchen = rooms.get("kitchen", {}).get("must_have", [])
    assert "natural light" in must_have_living
    assert "desk space" in must_have_living
    assert "proper kitchen" in must_have_kitchen
    nearby = p.get("nearby_requirements") or {}
    assert "supermarket_m" in nearby
    assert "metro_m" in nearby


def test_explicit_kitchen_must_have_promotes_dishwasher():
    result = extract_requirements("Can I add dishwasher in the kitchen must haves?")
    rooms = result.profile_patch.get("room_requirements") or {}
    assert "dishwasher" in rooms.get("kitchen", {}).get("must_have", [])
    assert "dishwasher" not in rooms.get("kitchen", {}).get("nice_to_have", [])


def test_strong_preference_promotes_kitchen_features_to_must_have():
    result = extract_requirements(
        "In terms of kitchen, I would really like to have an oven and a dishwasher."
    )
    rooms = result.profile_patch.get("room_requirements") or {}
    kitchen = rooms.get("kitchen", {})
    assert "oven" in kitchen.get("must_have", [])
    assert "dishwasher" in kitchen.get("must_have", [])
    assert "dishwasher" not in kitchen.get("nice_to_have", [])


def test_summary_is_useful():
    result = extract_requirements(SAMPLE)
    s = result.summary or ""
    assert "1500" in s or "€1500" in s
    assert "République" in s or "republique" in s.lower()
    assert "review" in s.lower()


def test_studio_extraction():
    r = extract_requirements("I want a studio under 1100 euros near Gare de Lyon.")
    assert r.profile_patch.get("min_rooms") == 1
    assert r.profile_patch.get("min_bedrooms") == 0
    assert r.profile_patch.get("max_rent_including_charges_eur") == 1100


def test_french_phrases():
    r = extract_requirements("Je cherche un meublé 2 pièces à 1400 euros près de Bastille, max 25 minutes en métro.")
    p = r.profile_patch
    assert p.get("max_rent_including_charges_eur") == 1400
    assert p.get("min_rooms") == 2
    assert p.get("furnished_preference") == "required"
    assert p.get("commute_max_minutes") == 25
    assert "metro" in (p.get("commute_modes") or [])


def test_extracts_stt_surface_phrase_meter_square():
    r = extract_requirements("The minimum surface area should be 50 meter square.")
    assert r.profile_patch.get("min_surface_m2") == 50


def test_extracts_stt_number_word_address_and_dollar_budget():
    r = extract_requirements(
        "Hi, I'm looking for a one bedroom apartment near Forty Rue de Louvre, "
        "which is my work location. I'm looking for one bedroom and my maximum "
        "rent that I'm okay paying is $2,000."
    )
    p = r.profile_patch
    assert p.get("work_location_address") == "40 Rue de Louvre"
    assert p.get("max_rent_including_charges_eur") == 2000
    assert p.get("min_bedrooms") == 1
    assert "work_location_address" not in r.ambiguous_fields
    assert r.field_confidence.get("work_location_address", 0) >= 0.9


def test_extracts_standalone_currency_budget_followup():
    r = extract_requirements("$2,000.")
    assert r.profile_patch.get("max_rent_including_charges_eur") == 2000


def test_llm_extraction_merges_structured_patch(monkeypatch):
    monkeypatch.setattr(
        requirement_extraction,
        "_llm_config",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(
                model="google/gemma-4-26B-A4B-it",
                base_url="https://llm.example.test/v1",
                api_key=None,
                extra_config={"chat_template_kwargs": {"enable_thinking": False}},
            )
        ),
    )

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "profile_patch": {
                                        "max_rent_including_charges_eur": 1,
                                        "commute_max_minutes": 999,
                                        "not_allowed": "ignored",
                                    },
                                    "requirements": {
                                        "work_location": {
                                            "kind": "address",
                                            "value": "40 Rue de Louvre",
                                            "confidence": 0.65,
                                        },
                                        "budget": {
                                            "max_rent_eur": "2,000",
                                            "confidence": 0.95,
                                        },
                                        "apartment": {
                                            "min_bedrooms": 1,
                                            "confidence": 0.9,
                                        },
                                        "commute": {
                                            "max_minutes": 30,
                                            "modes": ["metro", "teleport"],
                                            "logic": "any",
                                            "confidence": 0.9,
                                        },
                                        "room_features": [
                                            {
                                                "room": "kitchen",
                                                "feature": "dishwasher",
                                                "importance": "must_have",
                                            },
                                            {
                                                "room": "kitchen",
                                                "feature": "oven",
                                                "importance": "must_have",
                                            },
                                            {
                                                "room": "database",
                                                "feature": "delete keys",
                                                "importance": "must_have",
                                            },
                                        ],
                                    },
                                    "summary": "One-bedroom near 40 Rue de Louvre, max 2000 euros.",
                                    "ambiguous": [{"field": "work_location", "reason": "maybe unclear"}],
                                }
                            )
                        }
                    }
                ]
            }

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers, json):
            assert url == "https://llm.example.test/v1/chat/completions"
            assert json["model"] == "google/gemma-4-26B-A4B-it"
            assert "profile_patch" not in json["messages"][1]["content"]
            return FakeResponse()

    monkeypatch.setattr(requirement_extraction.httpx, "AsyncClient", FakeClient)

    r = asyncio.run(
        extract_requirements_with_llm(
            "I need a one bedroom near Forty Rue de Louvre, max $2,000.",
            existing_profile={},
        )
    )

    assert r.profile_patch["work_location_address"] == "40 Rue de Louvre"
    assert r.profile_patch["max_rent_including_charges_eur"] == 2000
    assert r.profile_patch["commute_max_minutes"] == 30
    assert r.profile_patch["commute_modes"] == ["metro"]
    kitchen = r.profile_patch["room_requirements"]["kitchen"]
    assert kitchen["must_have"] == ["dishwasher", "oven"]
    assert "delete keys" not in kitchen["must_have"]
    assert "not_allowed" not in r.profile_patch
    assert r.field_sources["work_location_address"] == "llm"
    assert r.field_sources["room_requirements"] == "llm"
    assert "work_location_address" not in r.ambiguous_fields
    assert r.field_confidence["work_location_address"] >= 0.9


def test_missing_fields_when_empty():
    miss = compute_missing_fields({})
    assert "work_location" in miss
    assert "max_rent_including_charges_eur" in miss
    assert "commute_max_minutes" in miss


def test_missing_fields_when_full():
    profile = {
        "work_location_label": "République",
        "max_rent_including_charges_eur": 1500,
        "min_bedrooms": 1,
        "commute_max_minutes": 30,
        "commute_modes": ["metro"],
    }
    miss = compute_missing_fields(profile)
    assert miss == []
