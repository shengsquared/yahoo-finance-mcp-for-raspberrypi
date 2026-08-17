# syntax=docker/dockerfile:1

# Every base image used here is published for linux/arm64, so this builds natively
# on 64-bit Raspberry Pi OS. To build it on another machine for a Pi, use:
#   docker buildx build --platform linux/arm64 -t yahoo-finance-mcp .

FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

COPY pyproject.toml uv.lock README.md server.py ./

# Build the virtual environment from the lock file so the image does not resolve
# dependencies (or compile them from source) on the Pi itself.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# Second stage: runtime image
FROM python:3.11-slim-bookworm

WORKDIR /app

RUN useradd --create-home --shell /usr/sbin/nologin mcp

COPY --from=builder --chown=mcp:mcp /app /app

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH" \
    YFINANCE_MCP_TRANSPORT=streamable-http \
    YFINANCE_MCP_HOST=0.0.0.0 \
    YFINANCE_MCP_PORT=8000 \
    YFINANCE_CACHE_DIR=/home/mcp/.cache/yahoo-finance-mcp

USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import os,socket; socket.create_connection(('127.0.0.1', int(os.environ['YFINANCE_MCP_PORT'])), 5).close()"]

# Serves over HTTP by default (see the env vars above). For a stdio client, run:
#   docker run -i --rm yahoo-finance-mcp yahoo-finance-mcp --transport stdio
CMD ["yahoo-finance-mcp"]
