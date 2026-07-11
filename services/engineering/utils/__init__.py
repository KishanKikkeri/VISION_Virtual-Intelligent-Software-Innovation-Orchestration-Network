"""services/engineering/utils — shared helpers used by every worker/lead/head."""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional


def parse_llm_json(raw: str, fallback: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Strips markdown code fences and parses JSON from an LLM response.
    Falls back to `fallback` (or {}) on any parse failure — never raises.
    """
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
    """
    Deterministic key so re-running the same worker for the same task
    produces a detectable duplicate rather than a silent double-write.
    """
    raw = f"{project_id}:{task_id}:{worker_agent_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def quality_gate(score: float, threshold: float = 0.7) -> bool:
    return score >= threshold


def exponential_backoff_seconds(retry_count: int, cap: int = 60) -> int:
    return min(cap, 2 ** max(0, retry_count))


def files_to_dicts(files: List[Any]) -> List[Dict[str, str]]:
    """Normalizes a list of CodeFile-like objects/dicts into plain dicts for the Repository Service payload."""
    out = []
    for f in files:
        if isinstance(f, dict):
            out.append({"path": f["path"], "content": f["content"], "mode": f.get("mode", "100644")})
        else:
            out.append({"path": f.path, "content": f.content, "mode": "100644"})
    return out


def summarize_failures(items: List[Dict[str, Any]], key: str = "failure_reason") -> str:
    reasons = [str(i.get(key)) for i in items if i.get(key)]
    return "; ".join(reasons[:5]) if reasons else "no failure detail available"
