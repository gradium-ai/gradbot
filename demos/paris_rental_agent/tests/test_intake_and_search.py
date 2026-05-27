"""End-to-end tests for sessions, intake, search, save/reject, drafts, isolation."""

from __future__ import annotations


def _guest_session(client):
    res = client.post("/api/auth/guest")
    assert res.status_code == 200, res.text
    return res.json()["id"]


def test_guest_session_logout_clears_cookie(client):
    user_id = _guest_session(client)
    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == user_id

    client.post("/api/auth/logout")
    me2 = client.get("/api/auth/me")
    assert me2.status_code == 401


def test_guest_cookie_session_restores_same_user(client):
    first = client.post("/api/auth/guest")
    assert first.status_code == 200, first.text
    first_user = first.json()

    me = client.get("/api/auth/me")
    assert me.status_code == 200
    assert me.json()["id"] == first_user["id"]

    second = client.post("/api/auth/guest")
    assert second.status_code == 200, second.text
    assert second.json()["id"] == first_user["id"]


def test_temporary_guest_session_uses_bearer_token_without_cookie(client):
    first = client.post("/api/auth/guest?persist=false")
    assert first.status_code == 200, first.text
    body = first.json()
    token = body["token"]
    assert body["persisted"] is False
    assert "set-cookie" not in first.headers

    without_token = client.get("/api/auth/me")
    assert without_token.status_code == 401

    headers = {"Authorization": f"Bearer {token}"}
    me = client.get("/api/auth/me", headers=headers)
    assert me.status_code == 200
    assert me.json()["id"] == body["id"]

    started = client.post("/api/intake/start", headers=headers)
    assert started.status_code == 200, started.text

    logout = client.post("/api/auth/logout", json={"forget": True}, headers=headers)
    assert logout.status_code == 200
    assert client.get("/api/auth/me", headers=headers).status_code == 401


def test_intake_full_flow_and_search_blocked(client):
    _guest_session(client)

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
    _guest_session(client)
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


def test_text_update_accepts_model_bedroom_alias(client):
    _guest_session(client)
    client.post("/api/intake/start")

    updated = client.post(
        "/api/intake/text-update",
        json={"patch": {"num_bedrooms": 2}},
    )

    assert updated.status_code == 200, updated.text
    assert updated.json()["draft_profile"]["min_bedrooms"] == 2


def test_chat_update_search_profile_opens_profile_editor(client):
    _guest_session(client)
    res = client.post(
        "/api/assistant/chat",
        json={"message": "Can I update my search profile?"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tool_calls"][0]["name"] == "open_profile_editor"
    assert "opened your search profile" in body["reply"]


def test_chat_show_more_apartments_lists_latest_matches(client):
    _guest_session(client)
    client.post("/api/intake/start")
    client.post(
        "/api/intake/transcript",
        json={"transcript": "Furnished 1-bedroom, max 1500 euros, my office is near République, 30 minutes by metro or bike."},
    )
    client.post("/api/intake/confirm")
    run = client.post("/api/search-runs", json={"max_results": 5})
    assert run.status_code == 200, run.text

    res = client.post(
        "/api/assistant/chat",
        json={"message": "Can you show me more apartments?"},
    )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["tool_calls"][0]["name"] == "list_top_matches"
    assert body["tool_calls"][0]["result"]["matches"]


def test_profile_update_hides_stale_matches_until_fresh_search(client):
    _guest_session(client)
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
    assert updated.json()["confirmation_status"] == "draft"
    stale = client.get("/api/matches").json()
    assert stale["matches"] == []
    assert stale["stale"] is True

    blocked = client.post("/api/search-runs", json={"max_results": 5})
    assert blocked.status_code == 409

    confirmed = client.post("/api/intake/confirm")
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["search_profile"]["confirmation_status"] == "confirmed"

    fresh = client.post("/api/search-runs", json={"max_results": 5})
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["matches"]
    after = client.get("/api/matches").json()
    assert after["matches"]
    assert after["stale"] is False


def test_legacy_invalid_min_rooms_is_repaired(client):
    from src.db import SessionLocal
    from src.models import SearchProfile, User

    user_id = _guest_session(client)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
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
        user = db.query(User).filter(User.id == user_id).one()
        sp = db.query(SearchProfile).filter(SearchProfile.user_id == user.id).one()
        assert sp.min_rooms is None
    finally:
        db.close()


def test_legacy_string_arrondissements_are_repaired(client):
    from src.db import SessionLocal
    from src.models import SearchProfile, User

    user_id = _guest_session(client)
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).one()
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
        user = db.query(User).filter(User.id == user_id).one()
        sp = db.query(SearchProfile).filter(SearchProfile.user_id == user.id).one()
        assert sp.preferred_arrondissements == [2, 3, 4]
        assert sp.excluded_arrondissements == [16]
    finally:
        db.close()


def test_text_update_coerces_human_arrondissement_values(client):
    _guest_session(client)
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
    _guest_session(client)
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
    assert "min_surface_m2" in res.json()["applied_fields"]


def test_voice_transcript_meter_square_updates_surface(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/transcript",
        json={"transcript": "The minimum surface area should be 50 meter square."},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_profile"]["min_surface_m2"] == 50
    assert "min_surface_m2" in body["applied_fields"]
    assert "min_surface_m2" not in body["ignored_fields"]


def test_voice_patch_min_surface_area_m2_alias_updates_surface(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"min_surface_area_m2": 50}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_profile"]["min_surface_m2"] == 50
    assert "min_surface_m2" in body["applied_fields"]


def test_voice_patch_max_commute_minutes_alias_updates_commute(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"max_commute_minutes": 40}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_profile"]["commute_max_minutes"] == 40
    assert "commute_max_minutes" in body["applied_fields"]
    assert "commute_max_minutes" not in body["ignored_fields"]


def test_voice_style_work_location_alias_updates_profile(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"work_location": "40 Rue de Louvre, 75002 Paris"}},
    )
    assert res.status_code == 200, res.text
    dp = res.json()["draft_profile"]
    assert dp["work_location_address"] == "40 Rue de Louvre, 75002 Paris"
    assert "work_location" not in res.json()["missing_fields"]


def test_room_requirement_patch_merges_with_existing_requirements(client):
    _guest_session(client)
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


def test_voice_patch_amenities_alias_updates_kitchen_must_haves(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"amenities": ["dishwasher", "oven"]}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    kitchen = body["draft_profile"]["room_requirements"]["kitchen"]
    assert "dishwasher" in kitchen["must_have"]
    assert "oven" in kitchen["must_have"]
    assert "room_requirements" in body["applied_fields"]
    assert "room_requirements" not in body["ignored_fields"]


def test_voice_patch_furnished_alias_updates_preference(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"furnished": True, "bedrooms": 1}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_profile"]["furnished_preference"] == "required"
    assert body["draft_profile"]["min_bedrooms"] == 1
    assert "furnished_preference" in body["applied_fields"]
    assert "min_bedrooms" in body["applied_fields"]


def test_voice_patch_furnished_preference_furnished_means_required(client):
    _guest_session(client)
    res = client.post(
        "/api/intake/text-update",
        json={"patch": {"furnished_preference": "furnished"}},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["draft_profile"]["furnished_preference"] == "required"
    assert "furnished_preference" in body["applied_fields"]


def test_user_data_isolation(client):
    from fastapi.testclient import TestClient

    # use a fresh client per user so cookies are isolated
    app = client.app
    c1 = TestClient(app)
    c2 = TestClient(app)
    e1 = _guest_session(c1)
    e2 = _guest_session(c2)
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
