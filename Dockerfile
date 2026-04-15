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

# Build the wheel targeting the runtime Python version
COPY --from=python:3.13-bookworm /usr/local/bin/python3.13 /usr/local/bin/python3.13
COPY --from=python:3.13-bookworm /usr/local/include/python3.13 /usr/local/include/python3.13
COPY --from=python:3.13-bookworm /usr/local/lib/libpython3.13.so* /usr/local/lib/
COPY --from=python:3.13-bookworm /usr/local/lib/python3.13 /usr/local/lib/python3.13
RUN ldconfig
RUN cd gradbot_py && uv run --with maturin maturin build --release --interpreter /usr/local/bin/python3.13 --out /app/dist

# Stage 2: Runtime image (no Rust toolchain)
FROM python:3.13-bookworm

# Node.js is required by the MCP demo (npx spawns MCP server subprocesses)
COPY --from=node:22-bookworm /usr/local/bin/node /usr/local/bin/node
COPY --from=node:22-bookworm /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -s ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -s ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY . .

# Copy the pre-built wheel
COPY --from=builder /app/dist /app/dist

# Install deps using the pre-built wheel instead of building from source
RUN cd demos && \
    sed -i '/\[tool\.uv\.sources\]/,$d' pyproject.toml && \
    uv sync --find-links /app/dist

EXPOSE 8000

ENV ROOT_PATH=""
CMD ["sh", "-c", "cd /app/demos && uv run --no-sync uvicorn app:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*' --root-path=${ROOT_PATH}"]
