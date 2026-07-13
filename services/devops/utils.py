"""services/devops/utils.py — shared helpers used across DevOps workers/leads/head."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

from services.devops.models import VersionBump


def parse_llm_json(raw: str, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    try:
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            parts = cleaned.split("```")
            cleaned = parts[1] if len(parts) > 1 else cleaned
            if cleaned.startswith("json"):
                cleaned = cleaned[4:]
        return json.loads(cleaned.strip())
    except Exception:
        return fallback if fallback is not None else {}


def idempotency_key(project_id: str, task_id: str, worker_agent_id: str) -> str:
    raw = f"{project_id}:{task_id}:{worker_agent_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def exponential_backoff_seconds(retry_count: int, cap: int = 60) -> int:
    return min(cap, 2 ** max(0, retry_count))


# -- Semantic versioning --------------------------------------------

_SEMVER_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_semver(version: str) -> Tuple[int, int, int]:
    m = _SEMVER_RE.match(version.strip())
    if not m:
        return (0, 1, 0)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def bump_version(previous_version: Optional[str], bump: VersionBump = VersionBump.PATCH) -> str:
    """
    Deterministic semantic-version bump. `previous_version=None` (first
    ever release) always returns "0.1.0" regardless of requested bump —
    there is nothing to bump from yet.
    """
    if not previous_version:
        return "0.1.0"

    major, minor, patch = parse_semver(previous_version)
    if bump == VersionBump.MAJOR:
        return f"{major + 1}.0.0"
    if bump == VersionBump.MINOR:
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


# -- Dockerfile / Compose / CI / Env rendering (deterministic) --------
# Deterministic templates stand in for an LLM call — Dockerfiles and
# docker-compose files for a fixed, known tech stack are highly
# mechanical, so a template avoids LLM nondeterminism for something
# that must build reproducibly every time. Mirrors the "deterministic
# gate" precedent already set by QA's coverage/perf estimators and
# Security's CVE/license tables.

def render_dockerfile(tech_stack: Dict[str, str], exposed_port: int = 8000) -> str:
    backend = (tech_stack or {}).get("backend", "Python+FastAPI")
    if "python" in backend.lower() or "fastapi" in backend.lower():
        return (
            "FROM python:3.12-slim\n"
            "WORKDIR /app\n"
            "COPY requirements.txt .\n"
            "RUN pip install --no-cache-dir -r requirements.txt\n"
            "COPY . .\n"
            f"EXPOSE {exposed_port}\n"
            f'CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "{exposed_port}"]\n'
        )
    return (
        "FROM node:20-slim\n"
        "WORKDIR /app\n"
        "COPY package*.json .\n"
        "RUN npm ci --omit=dev\n"
        "COPY . .\n"
        f"EXPOSE {exposed_port}\n"
        'CMD ["node", "index.js"]\n'
    )


def render_compose(project_name: str, exposed_port: int = 8000) -> Tuple[str, List[str]]:
    services = [project_name.lower().replace(" ", "-") or "app", "postgres", "nats"]
    content = (
        "version: \"3.9\"\n"
        "services:\n"
        f"  {services[0]}:\n"
        "    build: .\n"
        f"    ports:\n      - \"{exposed_port}:{exposed_port}\"\n"
        "    depends_on:\n      - postgres\n      - nats\n"
        "    environment:\n      - DATABASE_URL=postgresql://user:pass@postgres:5432/app\n"
        "  postgres:\n    image: postgres:16\n    environment:\n      - POSTGRES_PASSWORD=pass\n"
        "  nats:\n    image: nats:2-alpine\n"
    )
    return content, services


def render_github_actions(project_name: str) -> str:
    return (
        f"name: {project_name} CI/CD\n"
        "on:\n  push:\n    branches: [main, develop]\n  pull_request:\n    branches: [main]\n"
        "jobs:\n  build:\n    runs-on: ubuntu-latest\n    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - name: Build image\n        run: docker build -t app:${{ github.sha }} .\n"
        "      - name: Run tests\n        run: echo \"tests run in QA/Security phases\"\n"
    )


def render_env_example(openapi_spec: Dict[str, Any], database_schema: Dict[str, Any]) -> Tuple[str, Dict[str, str]]:
    variables = {
        "DATABASE_URL": "postgresql://user:pass@localhost:5432/app",
        "NATS_URL": "nats://localhost:4222",
        "APP_PORT": "8000",
        "LOG_LEVEL": "info",
    }
    if database_schema and database_schema.get("tables"):
        variables["DB_POOL_SIZE"] = "10"
    lines = [f"{k}={v}" for k, v in variables.items()]
    return "\n".join(lines) + "\n", variables
