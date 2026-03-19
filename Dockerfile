# Build the gradbot_py native extension and serve all demos.
#
# Usage:
#   docker build -t gradbot .
#   docker run -e GRADIUM_API_KEY=grd_... \
#              -e GRADIUM_BASE_URL=https://api.gradium.ai/api \
#              -e LLM_API_KEY=sk-... \
#              -e LLM_MODEL=gpt-4o \
#              -p 8000:8000 gradbot

# Stage 1: Build the native extension
FROM rust:1.90-bookworm AS builder

RUN apt-get update && apt-get install -y \
    python3-dev \
    python3-venv \
    pkg-config \
    cmake \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY Cargo.toml Cargo.lock* ./
COPY gradbot_lib gradbot_lib
COPY gradbot_py gradbot_py
COPY gradbot_server gradbot_server
COPY src src

# Build only the wheel using maturin directly (uses system Python, no extra download)
RUN cd gradbot_py && uv run --with maturin maturin build --release --out /app/dist

# Stage 2: Runtime image (no Rust toolchain)
FROM python:3.14-bookworm

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY . .

# Copy the pre-built wheel
COPY --from=builder /app/dist /app/dist

# Copy JS audio processor files into each demo's static/js
RUN for demo in demos/*/; do \
        [ -d "$demo/static" ] || continue; \
        cp -r gradbot_py/gradbot/js_audio_processor "$demo/static/js"; \
    done

# Install deps using the pre-built wheel instead of building from source
RUN cd demos && \
    sed -i '/\[tool\.uv\.sources\]/,$d' pyproject.toml && \
    uv sync --find-links /app/dist

EXPOSE 8000

ENV ROOT_PATH=""
CMD ["sh", "-c", "cd /app/demos && uv run --no-sync uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --root-path=${ROOT_PATH}"]
