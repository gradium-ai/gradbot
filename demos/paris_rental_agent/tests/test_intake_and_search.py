"""End-to-end tests for auth, intake, confirmation, search, save/reject, drafts, isolation."""

from __future__ import annotations

import uuid


def _signup(client, email=None, password="testpass123", name="Test User"):
    email = email or f"u{uuid.uuid4().hex[:8]}@example.com"
    res = client.post(
        "/api/auth/signup",
        json={"email": email, "password": password, "full_name": name},
    )
    assert res.status_code == 200, res.text
    return email


def test_signup_login_logout(client):
    email = _signup(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["email"] == email

    client.post("/api/auth/logout")
    me2 = client.get("/api/auth/me")
    assert me2.status_code == 401

    res = client.post("/api/auth/login", json={"email": email, "password": "testpass123"})
    assert res.status_code == 200


def test_intake_full_flow_and_search_blocked(client):
    _signup(client)

    start = client.post("/api/intake/start").json()
    assert start["ok"] is True
    assert start["confirmation_status"] == "draft"

    transcript = (
        "I'm looking for a furnished one-bedroom in Paris, max 1500 euros including charges, "
        "not more than 30 minutes from my office near République by metro or bike."
    )
    res = client.post("/api/intake/transcript", json={"transcript": transcript}).json()
    assert res["ok"] is True
    dp = res["draft_profile"]
    assert dp["max_rent_including_charges_eur"] == 1500
    assert dp["min_bedrooms"] == 1
    assert dp["commute_max_minutes"] == 30
    assert "metro" in dp["commute_modes"]
    assert dp["work_location_label"] and "République" in dp["work_location_label"]

    # Search must be blocked because profile is not confirmed
    blocked = client.post("/api/search-runs", json={"max_results": 5})
    assert blocked.status_code == 409
    body = blocked.json()
    assert body["detail"]["error"] == "search_profile_not_confirmed"

    # Text correction
    patched = client.post(
        "/api/intake/text-update",
        json={"patch": {"max_rent_including_charges_eur": 1450, "min_surface_m2": 32}},
    ).json()
    assert patched["draft_profile"]["max_rent_including_charges_eur"] == 1450
    assert patched["draft_profile"]["min_surface_m2"] == 32

    # Confirm
    confirmed = client.post("/api/intake/confirm").json()
    assert confirmed["ok"] is True
    assert confirmed["search_profile"]["confirmation_status"] == "confirmed"

    # Now search is allowed
    run = client.post("/api/search-runs", json={"max_results": 10})
    assert run.status_code == 200, run.text
    payload = run.json()
    assert payload["ok"] is True
    matches = payload["matches"]
    assert len(matches) > 0
    # Each match should expose a listing dict
    for m in matches:
        assert "listing" in m
        assert m["listing"]["title"]


def test_save_reject_draft_persistence(client):
    _signup(client)
    client.post("/api/intake/start")
    client.post(
        "/api/intake/transcript",
        json={"transcript": "Furnished 1-bedroom, max 1500 euros, my office is near République, 30 minutes by metro or bike."},
    )
    client.post("/api/intake/confirm")
    run = client.post("/api/search-runs", json={"max_results": 5}).json()
    assert run["ok"] is True
    listing_id = run["matches"][0]["listing_id"]

    saved = client.post(f"/api/listings/{listing_id}/save").json()
    assert saved["ok"] is True
    saved_list = client.get("/api/saved-listings").json()
    assert any(s["listing_id"] == listing_id for s in saved_list["saved_listings"])

    rejected_listing_id = run["matches"][1]["listing_id"]
    rej = client.post(f"/api/listings/{rejected_listing_id}/reject", json={"reason": "too small"}).json()
    assert rej["ok"] is True

    # Rejected listings should not be in matches
    matches_after = client.get("/api/matches").json()["matches"]
    assert all(m["listing_id"] != rejected_listing_id for m in matches_after)

    draft = client.post(
        f"/api/listings/{listing_id}/draft-viewing-request",
        json={"language": "fr"},
    ).json()
    assert draft["ok"] is True
    assert draft["draft"]["language"] == "fr"
    assert "Bonjour" in draft["draft"]["body"]

    drafts_list = client.get("/api/viewing-drafts").json()
    assert len(drafts_list["drafts"]) >= 1


def test_profile_update_hides_stale_matches_until_fresh_search(client):
    _signup(client)
    client.post("/api/intake/start")
    client.post(
        "/api/intake/transcript",
        json={"transcript": "Furnished 1-bedroom, max 1500 euros, my office is near République, 30 minutes by metro or bike."},
    )
    client.post("/api/intake/confirm")
    run = client.post("/api/search-runs", json={"max_results": 5})
    assert run.status_code == 200, run.text
    assert run.json()["matches"]

    before = client.get("/api/matches").json()
    assert before["matches"]
    assert before["stale"] is False

    updated = client.post(
        "/api/intake/text-update",
        json={"patch": {"max_rent_including_charges_eur": 1800}},
    )
    assert updated.status_code == 200, updated.text
    stale = client.get("/api/matches").json()
    assert stale["matches"] == []
    assert stale["stale"] is True

    fresh = client.post("/api/search-runs", json={"max_results": 5})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["matches"]
    after = client.get("/api/matches").json()
    assert after["matches"]
    assert after["stale"] is False


def test_legacy_invalid_min_rooms_is_repaired(client):
    from app.db import SessionLocal
    from app.models import SearchProfile, User

    email = _signup(client)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        sp = db.query(SearchProfile).filter(SearchProfile.user_id == user.id).one()
        sp.min_rooms = 0
        db.commit()
    finally:
        db.close()

    res = client.get("/api/search-profile")
    assert res.status_code == 200, res.text
    assert res.json()["min_rooms"] is None

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        sp = db.query(SearchProfile).filter(SearchProfile.user_id == user.id).one()
        assert sp.min_rooms is None
    finally:
        db.close()


def test_legacy_string_arrondissements_are_repaired(client):
    from app.db import SessionLocal
    from app.models import SearchProfile, User

    email = _signup(client)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        sp = db.query(SearchProfile).filter(SearchProfile.user_id == user.id).one()
        sp.preferred_arrondissements = ["2nd", "3rd", "75004"]
        sp.excluded_arrondissements = ["16th"]
        db.commit()
    finally:
        db.close()

    res = client.get("/api/search-profile")
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["preferred_arrondissements"] == [2, 3, 4]
    assert body["excluded_arrondissements"] == [16]

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.email == email).one()
        sp = db.query(SearchProfile).filter(SearchProfile.user_id == user.id).one()
        assert sp.preferred_arrondissements == [2, 3, 4]
        assert sp.excluded_arrondissements == [16]
    finally:
        db.close()


def test_text_update_coerces_human_arrondissement_values(client):
    _signup(client)
    res = client.post(
        "/api/intake/text-update",
        json={
            "patch": {
                "preferred_arrondissements": ["2nd", "3rd", "4th arrondissement"]
            }
        },
    )
    assert res.status_code == 200, res.text
    assert res.json()["draft_profile"]["preferred_arrondissements"] == [2, 3, 4]


def test_text_update_coerces_common_llm_field_aliases(client):
    _signup(client)
    res = client.post(
        "/api/intake/text-update",
        json={
            "patch": {
                "max_budget": 1800,
                "minimum_surface_area_sqm": 45,
                "preferred_arrondissement": ["75002", "13th"],
            }
        },
    )
    assert res.status_code == 200, res.text
    dp = res.json()["draft_profile"]
    assert dp["max_rent_including_charges_eur"] == 1800
    assert dp["min_surface_m2"] == 45
    assert dp["preferred_arrondissements"] == [2, 13]


def test_voice_style_work_location_alias_updates_profile(client):
    _signup(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"work_location": "40 Rue de Louvre, 75002 Paris"}},
    )
    assert res.status_code == 200, res.text
    dp = res.json()["draft_profile"]
    assert dp["work_location_address"] == "40 Rue de Louvre, 75002 Paris"
    assert "work_location" not in res.json()["missing_fields"]


def test_room_requirement_patch_merges_with_existing_requirements(client):
    _signup(client)
    first = client.post(
        "/api/intake/text-update",
        json={
            "patch": {
                "room_requirements": {
                    "living_room": {"must_have": ["natural light"], "nice_to_have": []}
                }
            }
        },
    )
    assert first.status_code == 200, first.text

    second = client.post(
        "/api/intake/text-update",
        json={
            "patch": {
                "room_requirements": {
                    "kitchen": {"must_have": ["dishwasher"], "nice_to_have": []}
                }
            }
        },
    )
    assert second.status_code == 200, second.text
    rooms = second.json()["draft_profile"]["room_requirements"]
    assert rooms["living_room"]["must_have"] == ["natural light"]
    assert rooms["kitchen"]["must_have"] == ["dishwasher"]


def test_user_data_isolation(client):
    from fastapi.testclient import TestClient

    # use a fresh client per user so cookies are isolated
    app = client.app
    c1 = TestClient(app)
    c2 = TestClient(app)
    e1 = _signup(c1)
    e2 = _signup(c2)
    assert e1 != e2

    c1.post("/api/intake/start")
    c1.post(
        "/api/intake/transcript",
        json={"transcript": "Furnished 1-bedroom, max 1500 euros, my office is near République, 30 minutes by metro or bike."},
    )
    c1.post("/api/intake/confirm")
    run1 = c1.post("/api/search-runs", json={"max_results": 5}).json()
    listing_id = run1["matches"][0]["listing_id"]
    c1.post(f"/api/listings/{listing_id}/save")

    saved1 = c1.get("/api/saved-listings").json()
    saved2 = c2.get("/api/saved-listings").json()
    assert any(s["listing_id"] == listing_id for s in saved1["saved_listings"])
    assert all(s["listing_id"] != listing_id for s in saved2.get("saved_listings", []))

    # User 2 should not see user 1's matches
    matches2 = c2.get("/api/matches").json()
    assert matches2["matches"] == []
