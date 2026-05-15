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
