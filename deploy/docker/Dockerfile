# deploy/docker/Dockerfile
# =========================================
# M4.9 §12 — the single application image used by every Compose file
# and the Helm chart's `image.repository`/`image.tag` (default
# `aasc-platform`). Template only: no secrets baked in, config is
# entirely environment-variable driven per `deployment_profiles.py`.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN useradd --create-home --shell /bin/bash aasc && chown -R aasc:aasc /app
USER aasc

EXPOSE 8000 8443

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
