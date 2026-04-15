# Contributing to Gradbot

Thanks for taking the time to contribute. This document explains how to set up the project, the workflow we use, and what we look for in pull requests.

## Getting started

### Prerequisites

- Rust (stable) — install via [rustup](https://rustup.rs/)
- Python 3.12+ — for the Python bindings and demos
- [uv](https://docs.astral.sh/uv/) — Python package and project manager

### Local setup

```bash
git clone https://github.com/gradium-ai/gradbot.git
cd gradbot

# Build the Rust workspace
cargo build

# Run the Python tests
cd gradbot_py
uv sync
uv run pytest
```

To work on a demo:

```bash
cd demos/simple_chat
uv sync
uv run uvicorn main:app --reload
```

### Rebuilding the native extension after Rust changes

The Python demos depend on the `gradbot` wheel built from the Rust code. After modifying Rust:

```bash
make build DEMO=simple_chat   # rebuild + reinstall into one demo's venv
make build-all                # rebuild + reinstall into every demo's venv
```

## Branching and commits

- Open a branch off `main` for each piece of work.
- Keep commits small and focused. Each commit should leave the tree in a working state.
- Write commit messages that explain the *why*, not just the *what*. The first line is a short summary; add a paragraph below if context helps.
- Run `cargo fmt`, `cargo clippy`, and `cargo test` before pushing.

## Pull requests

- One logical change per PR.
- Include a short description of what the PR does and why.
- If the PR changes user-facing behavior, update the relevant README or example.
- If you're adding a new demo, follow the structure of existing demos (`main.py` + `static/index.html` + `pyproject.toml`).

## Reporting bugs and asking questions

Open a GitHub issue with a minimal reproduction. For voice/audio bugs, include the OS, browser, and which demo you were running.

## License

By contributing, you agree that your contributions will be dual-licensed under the [MIT](LICENSE-MIT) and [Apache-2.0](LICENSE-APACHE) licenses, matching the project as a whole.
