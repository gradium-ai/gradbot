"""
Playwright E2E test for the npc_3d_game win condition.

Tests that solving all 3 clues triggers the victory sequence.
Bypasses the 3D game by using the exposed window.__test hooks
to directly manipulate the clue system.

Run:
  cd demos/npc_3d_game
  .venv/bin/python -m pytest tests/test_win_condition.py -v
"""

import subprocess
import time
import signal
import pytest
from playwright.sync_api import sync_playwright

SERVER_PORT = 8765  # Use a non-default port to avoid conflicts


@pytest.fixture(scope="module")
def server():
    """Start the uvicorn server for the duration of the test module."""
    proc = subprocess.Popen(
        [
            ".venv/bin/uvicorn", "main:app",
            "--port", str(SERVER_PORT),
            "--log-level", "warning",
        ],
        cwd="demos/npc_3d_game",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    # Wait for server to be ready
    import urllib.request
    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://localhost:{SERVER_PORT}/health")
            break
        except Exception:
            time.sleep(0.5)
    else:
        proc.kill()
        raise RuntimeError("Server did not start in time")

    yield proc

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture
def page(server):
    """Create a browser page for each test."""
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        ctx = browser.new_context()
        pg = ctx.new_page()
        pg.goto(f"http://localhost:{SERVER_PORT}/")
        # Wait for the game to load (the start overlay should be visible)
        pg.wait_for_selector("#start-overlay, canvas", timeout=10000)
        # Give ES modules time to initialize
        time.sleep(2)
        yield pg
        browser.close()


def test_clue_system_tracks_solves(page):
    """Test that marking clues as solved increments the counter."""
    # Start the game — click overlay if visible, or force-click via JS
    page.evaluate("""() => {
        const overlay = document.querySelector('#start-overlay');
        if (overlay && overlay.style.display !== 'none') {
            overlay.click();
        }
    }""")
    page.wait_for_function("window.__test && window.__test.clueSystem", timeout=60000)

    result = page.evaluate("""() => {
        const cs = window.__test.clueSystem;
        return { solved: cs.solved, total: cs.total };
    }""")
    assert result["total"] == 3
    assert result["solved"] == 0


def test_win_condition_triggers_on_all_clues_solved(page):
    """Test that solving all 3 clues triggers the victory UI."""
    # Start the game if not started
    page.evaluate("""() => {
        if (!window.__test) {
            document.querySelector('#start-overlay')?.click();
        }
    }""")
    page.wait_for_function("window.__test && window.__test.clueSystem", timeout=60000)

    # Directly mark all clues as solved and trigger win check
    victory_triggered = page.evaluate("""() => {
        const cs = window.__test.clueSystem;

        // Manually solve each clue (bypass voice sessions)
        for (const [id, clue] of cs._clues) {
            if (!clue.solved) {
                clue.solved = true;
                cs._solvedCount++;
            }
        }

        // Verify counts
        const counts = { solved: cs.solved, total: cs.total };

        // Trigger win condition check
        window.__test.checkWinCondition();

        return counts;
    }""")

    assert victory_triggered["solved"] == 3
    assert victory_triggered["total"] == 3

    # Wait for victory UI to appear (showVictory is called after 10s setTimeout)
    page.wait_for_selector("#victory-title", state="visible", timeout=15000)

    victory_text = page.text_content("#victory-title")
    assert victory_text is not None
    assert len(victory_text) > 0


def test_win_condition_does_not_trigger_with_partial_solves(page):
    """Test that solving only 2/3 clues does NOT trigger victory."""
    page.evaluate("""() => {
        if (!window.__test) {
            document.querySelector('#start-overlay')?.click();
        }
    }""")
    page.wait_for_function("window.__test && window.__test.clueSystem", timeout=60000)

    result = page.evaluate("""() => {
        const cs = window.__test.clueSystem;

        // Solve only 2 clues
        let count = 0;
        for (const [id, clue] of cs._clues) {
            if (count >= 2) break;
            if (!clue.solved) {
                clue.solved = true;
                cs._solvedCount++;
                count++;
            }
        }

        window.__test.checkWinCondition();

        return { solved: cs.solved, total: cs.total };
    }""")

    assert result["solved"] == 2
    assert result["total"] == 3

    # Victory should NOT appear
    victory = page.query_selector("#victory-title")
    is_visible = page.evaluate("""(el) => {
        if (!el) return false;
        const style = window.getComputedStyle(el.closest('[style*="display"]') || el);
        return style.display !== 'none';
    }""", victory) if victory else False

    assert not is_visible, "Victory should not be visible with only 2/3 clues solved"
