"""Unit tests for the deterministic helpers in main.py — grid-cell parsing,
egocentric direction phrasing, and speech composition. These are the
functions the module's own docstrings insist stay "exact code, never left to
a model," so they're the highest-value place to add coverage.
"""

import fastapi
import pytest

from main import (
    GRID_COLS,
    GRID_ROWS,
    _b64_payload,
    _build_speech,
    _check_speech,
    _compose,
    _egocentric_phrase,
    _infer_missing_page_numbers,
    _location_phrase,
    _needs_escalation,
    _num_word,
    _parse_box,
    _parse_cell,
    _piece_cells,
    _piece_name,
    _speakable,
    _to_int,
)


def test_grid_is_3x3():
    # The direction math below assumes a 3x3 grid; if this ever changes the
    # phrase tables in _egocentric_phrase need to change with it.
    assert (GRID_ROWS, GRID_COLS) == (3, 3)


# ── _num_word / _speakable ────────────────────────────────────────────────

def test_num_word_known_and_out_of_range():
    assert _num_word(0) == "zero"
    assert _num_word(4) == "four"
    assert _num_word(10) == "ten"
    assert _num_word(11) == "11"
    assert _num_word(-1) == "-1"


def test_speakable_rewrites_dimension_notation():
    assert _speakable("2x4 brick") == "2 by 4 brick"
    assert _speakable("1×1 plate") == "1 by 1 plate"
    assert _speakable("a red brick") == "a red brick"


# ── _parse_cell ───────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "cell,expected",
    [
        ("A1", (0, 0)),
        ("C3", (2, 2)),
        ("b2", (1, 1)),
        (" A1 ", (0, 0)),
        ("A4", None),  # column out of range for a 3-wide grid
        ("D1", None),  # row out of range for a 3-tall grid
        ("1A", None),  # wrong shape
        (None, None),
        ("", None),
    ],
)
def test_parse_cell(cell, expected):
    assert _parse_cell(cell) == expected


# ── _parse_box ────────────────────────────────────────────────────────────

def test_parse_box_valid():
    assert _parse_box([0.1, 0.2, 0.3, 0.4]) == [0.1, 0.2, 0.3, 0.4]


def test_parse_box_clamps_out_of_range_values():
    assert _parse_box([-0.5, 0.2, 1.5, 0.4]) == [0.0, 0.2, 1.0, 0.4]


@pytest.mark.parametrize(
    "box",
    [
        [0.1, 0.1, 0.104, 0.5],  # width < 0.005
        [0.1, 0.1, 0.5, 0.104],  # height < 0.005
        [0.1, 0.2, 0.3],  # wrong length
        "not-a-box",
        [0.1, 0.2, "nope", 0.4],
    ],
)
def test_parse_box_rejects_malformed_or_degenerate(box):
    assert _parse_box(box) is None


# ── _piece_cells ──────────────────────────────────────────────────────────

def test_piece_cells_keeps_box_when_centroid_matches_cell():
    piece = {"copies": [{"cell": "A1", "box": [0.0, 0.0, 0.3, 0.3]}]}
    cells = _piece_cells(piece)
    assert cells == [{"row": 0, "col": 0, "label": "A1", "box": [0.0, 0.0, 0.3, 0.3]}]


def test_piece_cells_drops_box_when_centroid_mismatches_cell():
    # Box centroid (~0.83, ~0.83) lands in cell C3, not the reported A1.
    piece = {"copies": [{"cell": "A1", "box": [0.7, 0.7, 0.95, 0.95]}]}
    cells = _piece_cells(piece)
    assert cells == [{"row": 0, "col": 0, "label": "A1"}]


def test_piece_cells_skips_unparseable_entries():
    piece = {"copies": [{"cell": "Z9"}, {"cell": "B2"}]}
    cells = _piece_cells(piece)
    assert cells == [{"row": 1, "col": 1, "label": "B2"}]


def test_piece_cells_falls_back_to_legacy_grid_cells():
    assert _piece_cells({"grid_cells": ["B2"]}) == [
        {"row": 1, "col": 1, "label": "B2"}
    ]


def test_piece_cells_falls_back_to_legacy_singular_grid_cell():
    assert _piece_cells({"grid_cell": "C3"}) == [
        {"row": 2, "col": 2, "label": "C3"}
    ]


def test_piece_cells_empty_when_nothing_present():
    assert _piece_cells({}) == []


# ── _egocentric_phrase / _location_phrase ────────────────────────────────

def test_egocentric_phrase_facing_orientation():
    assert _egocentric_phrase(0, 0, "facing") == "close to you, on your right"
    assert _egocentric_phrase(2, 2, "facing") == "at the far side, on your left"
    assert _egocentric_phrase(1, 1, "facing") == "dead center of the pile"


def test_egocentric_phrase_overhead_orientation_does_not_flip_sides():
    assert _egocentric_phrase(0, 0, "overhead") == "at the far side, on your left"


def test_location_phrase_single_cell_has_no_count_prefix():
    cells = [{"row": 0, "col": 0}]
    assert _location_phrase(cells, "facing") == "close to you, on your right"


def test_location_phrase_groups_matching_directions():
    cells = [{"row": 0, "col": 0}, {"row": 0, "col": 0}]
    assert _location_phrase(cells, "facing") == "two close to you, on your right"


def test_location_phrase_caps_at_three_groups():
    cells = [
        {"row": 0, "col": 0},  # close to you, on your right
        {"row": 0, "col": 2},  # close to you, on your left
        {"row": 2, "col": 0},  # at the far side, on your right
        {"row": 2, "col": 2},  # at the far side, on your left
    ]
    phrase = _location_phrase(cells, "facing")
    assert phrase == (
        "one close to you, on your right; one close to you, on your left; "
        "one at the far side, on your right; and more elsewhere"
    )


# ── _piece_name ───────────────────────────────────────────────────────────

def test_piece_name_joins_color_and_description():
    assert _piece_name({"color": "red", "description": "2x4 brick"}) == "red 2 by 4 brick"


def test_piece_name_avoids_double_speaking_color():
    assert _piece_name({"color": "red", "description": "red 2x4 brick"}) == "red 2 by 4 brick"


def test_piece_name_defaults_description_to_piece():
    assert _piece_name({}) == "piece"


# ── _needs_escalation ─────────────────────────────────────────────────────

def test_needs_escalation_true_when_a_piece_has_no_location():
    result = {"pieces": [{"copies": [{"cell": "A1"}]}, {"copies": []}]}
    assert _needs_escalation(result) is True


def test_needs_escalation_false_when_every_piece_located():
    result = {"pieces": [{"copies": [{"cell": "A1"}]}]}
    assert _needs_escalation(result) is False


def test_needs_escalation_false_for_empty_pieces_list():
    assert _needs_escalation({"pieces": []}) is False
    assert _needs_escalation({}) is False


# ── _build_speech / _compose ──────────────────────────────────────────────

def test_build_speech_reports_missing_pieces():
    result = {"pieces": []}
    assert _build_speech(result, None, "facing") == "I couldn't read a parts list for that step."


def test_build_speech_prefixes_step_number():
    result = {"notes": "nothing found"}
    step = {"step": 3}
    assert _build_speech(result, step, "facing") == "Step 3: nothing found"


def test_build_speech_pluralizes_and_locates_pieces():
    result = {
        "pieces": [
            {"quantity": 2, "color": "red", "description": "2x4 brick",
             "copies": [{"cell": "A1"}]},
        ]
    }
    speech = _build_speech(result, None, "facing")
    assert speech == (
        "You need two red 2 by 4 bricks — close to you, on your right."
    )


def test_build_speech_reports_unspotted_piece():
    result = {"pieces": [{"quantity": 1, "description": "1x1 plate", "copies": []}]}
    speech = _build_speech(result, None, "facing")
    assert "I couldn't spot that one" in speech


def test_compose_adds_cells_phrase_and_summary():
    result = {"pieces": [{"quantity": 1, "description": "plate", "copies": [{"cell": "A1"}]}]}
    composed = _compose(result, None, "facing")
    assert composed["pieces"][0]["phrase"] == "close to you, on your right"
    assert "summary" in composed


# ── _check_speech ─────────────────────────────────────────────────────────

def test_check_speech_confident_match_includes_quantity():
    result = {"verdict": "match", "match_index": 0, "confidence": 0.9}
    pieces = [{"description": "2x4 brick", "quantity": 3}]
    speech = _check_speech(result, pieces)
    assert speech.startswith("Yes — that's the 2 by 4 brick.")
    assert "three of those" in speech


def test_check_speech_low_confidence_match_hedges():
    result = {"verdict": "match", "match_index": 0, "confidence": 0.3}
    pieces = [{"description": "plate", "quantity": 1}]
    speech = _check_speech(result, pieces)
    assert speech.startswith("That looks like the plate to me.")


def test_check_speech_different_lists_alternatives():
    result = {"verdict": "different"}
    pieces = [{"description": "plate"}, {"description": "brick"}]
    speech = _check_speech(result, pieces)
    assert "not one of this step's pieces" in speech
    assert "plate or the brick" in speech


def test_check_speech_unsure_asks_to_get_closer():
    result = {"verdict": "unsure"}
    assert "hold it a bit closer" in _check_speech(result, [])


# ── _b64_payload ──────────────────────────────────────────────────────────

def test_b64_payload_accepts_bare_base64():
    assert _b64_payload("aGVsbG8=", "pile") == "aGVsbG8="


def test_b64_payload_strips_data_url_prefix():
    assert _b64_payload("data:image/jpeg;base64,aGVsbG8=", "pile") == "aGVsbG8="


def test_b64_payload_rejects_invalid_base64():
    with pytest.raises(fastapi.HTTPException) as exc_info:
        _b64_payload("not base64 at all!!", "pile")
    assert exc_info.value.status_code == 400


# ── _infer_missing_page_numbers ────────────────────────────────────────────

def test_infer_missing_page_numbers_fills_from_neighbours():
    pages = [{"page": 1}, {"page": None}, {"page": None}, {"page": 4}]
    _infer_missing_page_numbers(pages)
    assert [p["page"] for p in pages] == [1, 2, 3, 4]


def test_infer_missing_page_numbers_extrapolates_leading_and_trailing_gaps():
    pages = [{"page": None}, {"page": 2}, {"page": None}]
    _infer_missing_page_numbers(pages)
    assert [p["page"] for p in pages] == [1, 2, 3]


def test_infer_missing_page_numbers_leaves_all_none_when_no_anchor():
    pages = [{"page": None}, {"page": None}]
    _infer_missing_page_numbers(pages)
    assert [p["page"] for p in pages] == [None, None]


# ── _to_int ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (3, 3),
        ("3", 3),
        (" 42 ", 42),
        ("not a number", None),
        (None, None),
    ],
)
def test_to_int(value, expected):
    assert _to_int(value) == expected
