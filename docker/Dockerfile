FROM python:3.12-slim

# ── System deps ───────────────────────────────────────────────
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# ── App user (non-root) ───────────────────────────────────────
RUN useradd -m -u 1000 aasc
WORKDIR /app

# ── Python deps ───────────────────────────────────────────────
COPY pyproject.toml .
RUN pip install --no-cache-dir --upgrade pip \
    && pip install --no-cache-dir -e ".[dev]"

# ── Source ────────────────────────────────────────────────────
COPY . .
RUN mkdir -p /app/data/artifacts && chown -R aasc:aasc /app

USER aasc

# ── Migrate + start ───────────────────────────────────────────
CMD ["sh", "-c", "alembic upgrade head && uvicorn apps.demo.main:app --host 0.0.0.0 --port 8000 --workers 1"]
