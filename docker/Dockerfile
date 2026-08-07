# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:python3.14-bookworm-slim AS builder

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never

WORKDIR /app

# Dependencies resolve in their own layer so editing source does not reinstall
# the world.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-install-project --no-dev

COPY pyproject.toml uv.lock README.md ./
COPY src ./src

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev


FROM python:3.14-slim-bookworm AS runtime

RUN groupadd --system app && useradd --system --gid app --create-home app

COPY --from=builder --chown=app:app /app /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    DPMP_DATA_DIR=/data

# The built feed lives on a volume: a cold start would otherwise have to crawl
# ~2,700 trips before the service could answer anything.
RUN mkdir -p /data && chown app:app /data
VOLUME ["/data"]

USER app
WORKDIR /app
EXPOSE 8000

# Reports unhealthy when either feed goes stale, not merely when the process
# is alive -- a service serving a two-hour-old realtime feed is not working.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=4).status == 200 else 1)"

CMD ["dpmp-gtfs", "serve", "--host", "0.0.0.0", "--port", "8000"]
