FROM python:3.12-slim-bookworm

# Pin uv independently from Python package dependencies in uv.lock.
COPY --from=ghcr.io/astral-sh/uv:0.13.6 /uv /uvx /bin/

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    HF_HOME=/home/transcriber/.cache/huggingface \
    TMPDIR=/tmp \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates libgomp1 \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 transcriber \
    && useradd --uid 10001 --gid transcriber --create-home --shell /usr/sbin/nologin transcriber \
    && mkdir -p /home/transcriber/.cache/huggingface \
    && chown -R transcriber:transcriber /home/transcriber

# Resolve locked runtime dependencies separately so source edits do not reinstall them.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project --no-cache

COPY --chown=10001:10001 src/ ./src/
RUN uv sync --locked --no-dev --no-cache --no-editable

USER 10001:10001

EXPOSE 8000

# Readiness waits for model initialization, which can include a first-run download.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15m --retries=3 \
    CMD ["python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:8000/health/ready', timeout=2)"]

CMD ["uvicorn", "transcription_service.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
