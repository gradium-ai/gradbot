"""Tests for requirement extraction."""

from __future__ import annotations

from app.services.requirement_extraction import (
    extract_requirements,
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
