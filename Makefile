DEMOS := business_bank fantasy_shop hotel restaurant_ordering \
         simple_chat threejs_game

MATURIN := uv tool run maturin
MANIFEST := -m gradbot_py/Cargo.toml
WHEEL_DIR := /tmp/gradbot_wheel

# Build and install gradbot_py into a specific demo's venv
# Usage: make build DEMO=threejs_game
build:
ifndef DEMO
	$(error DEMO is required. Usage: make build DEMO=threejs_game)
endif
	@rm -rf $(WHEEL_DIR)
	$(MATURIN) build $(MANIFEST) -o $(WHEEL_DIR)
	demos/$(DEMO)/.venv/bin/python3 -m pip install $(WHEEL_DIR)/gradbot-*.whl --force-reinstall --no-deps

# Build and install into all demo venvs
build-all:
	@rm -rf $(WHEEL_DIR)
	$(MATURIN) build $(MANIFEST) -o $(WHEEL_DIR)
	@for demo in $(DEMOS); do \
		echo "==> Installing for $$demo"; \
		demos/$$demo/.venv/bin/pip install $(WHEEL_DIR)/gradbot-*.whl --force-reinstall --no-deps || exit 1; \
	done

# Run a demo with uvicorn
# Usage: make run DEMO=threejs_game [PORT=8000]
PORT ?= 8000
run: build
	cd demos/$(DEMO) && .venv/bin/uvicorn main:app --port $(PORT)

.PHONY: build build-all run
