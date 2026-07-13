"""services/security/utils — shared helpers used by every Security worker/lead/head."""
from __future__ import annotations

import hashlib
import json
import re
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
    """Deterministic key so re-running the same worker for the same task is detectable."""
    raw = f"{project_id}:{task_id}:{worker_agent_id}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def finding_id_for(project_id: str, category: str, module_id: Optional[str]) -> str:
    """Deterministic finding id so re-detecting the same issue doesn't create duplicates."""
    raw = f"{project_id}:{category}:{module_id or 'unknown'}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def quality_gate(score: float, threshold: float = 0.7) -> bool:
    return score >= threshold


def exponential_backoff_seconds(retry_count: int, cap: int = 60) -> int:
    return min(cap, 2 ** max(0, retry_count))


def severity_for_category(category: str) -> str:
    """
    Maps a finding category to a default FindingSeverity, per the spec's
    Hard Fail / Warning condition split:
      Hard Fail: critical vulnerability, secret detected, critical CVE,
                 compliance violation, high-risk dependency  -> critical/high
      Warning:   medium CVE, deprecated dependency, license warning,
                 low severity issue  -> medium/low
    """
    hard_fail = {
        "secret": "critical",
        "cve": "high",
        "compliance_violation": "high",
        "high_risk_dependency": "high",
    }
    return hard_fail.get(category, "medium")


# ── Dependency manifest extraction ─────────────────────────────

_REQUIREMENTS_LINE_RE = re.compile(
    r"^\s*([A-Za-z0-9_.\-]+)\s*(?:==|>=|<=|~=|>|<)\s*([A-Za-z0-9_.\-]+)"
)
_PACKAGE_JSON_DEP_RE = re.compile(r'"([A-Za-z0-9_.\-@/]+)"\s*:\s*"([^"]+)"')


def extract_dependencies_from_source(files: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """
    Best-effort extraction of a Dependency Manifest from Engineering's
    source_code artifact. Recognizes requirements.txt (pypi) and
    package.json "dependencies"/"devDependencies" blocks (npm).

    Design note (docs/M3.5_Security_Service_Handover.md): the platform
    does not yet produce a standalone `dependency_manifest` artifact
    (Architecture/Engineering don't emit one), so Security derives it
    directly from source_code files rather than assuming an artifact
    that doesn't exist — the same "don't assume a nonexistent endpoint"
    convention M3.4 used for `get_commit_history`.
    """
    deps: List[Dict[str, str]] = []
    seen = set()

    for f in files:
        path = (f.get("path") or "") if isinstance(f, dict) else ""
        content = (f.get("content") or "") if isinstance(f, dict) else ""
        name_lower = path.lower().split("/")[-1]

        if name_lower == "requirements.txt":
            for line in content.splitlines():
                m = _REQUIREMENTS_LINE_RE.match(line)
                if m:
                    name, version = m.group(1), m.group(2)
                    key = ("pypi", name.lower())
                    if key not in seen:
                        seen.add(key)
                        deps.append({"name": name, "version": version, "ecosystem": "pypi"})

        elif name_lower == "package.json":
            in_deps_block = False
            for line in content.splitlines():
                if '"dependencies"' in line or '"devDependencies"' in line:
                    in_deps_block = True
                    continue
                if in_deps_block:
                    if "}" in line:
                        in_deps_block = False
                        continue
                    m = _PACKAGE_JSON_DEP_RE.search(line)
                    if m:
                        name, version = m.group(1), m.group(2)
                        key = ("npm", name.lower())
                        if key not in seen:
                            seen.add(key)
                            deps.append({"name": name, "version": version.lstrip("^~"), "ecosystem": "npm"})

    return deps


# ── Secret scanning ─────────────────────────────────────────────

SECRET_PATTERNS: Dict[str, re.Pattern] = {
    "aws_access_key":  re.compile(r"AKIA[0-9A-Z]{16}"),
    "private_key":     re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "generic_api_key": re.compile(r"""(?i)(?:api[_-]?key|secret|token)\s*[=:]\s*['"][A-Za-z0-9_\-]{16,}['"]"""),
    "slack_token":     re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
}


def scan_content_for_secrets(path: str, content: str) -> List[Dict[str, Any]]:
    """Deterministic regex-based secret scan over a single file's content."""
    hits: List[Dict[str, Any]] = []
    lines = content.splitlines()
    for rule, pattern in SECRET_PATTERNS.items():
        for i, line in enumerate(lines, start=1):
            if pattern.search(line):
                hits.append({"file": path, "rule": rule, "line": i})
    return hits


# ── License classification ──────────────────────────────────────

# A small, deterministic allow/deny list. Anything not recognized is
# reported under "unknown" and treated as a warning, not a hard fail.
PERMISSIVE_LICENSES = {"MIT", "Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "ISC"}
DISALLOWED_LICENSES = {"GPL-3.0", "AGPL-3.0"}


# Deterministic package -> license lookup, standing in for a live
# license-metadata feed (mirrors KNOWN_VULNERABLE_PACKAGES below).
LICENSE_TABLE: Dict[str, str] = {
    "requests": "Apache-2.0", "pyyaml": "MIT", "django": "BSD-3-Clause",
    "flask": "BSD-3-Clause", "lodash": "MIT", "express": "MIT",
    "minimist": "MIT", "log4j": "Apache-2.0",
    "some-gpl-lib": "GPL-3.0",   # deliberately disallowed, for gate testing
}


def classify_license(name: str) -> str:
    if name in DISALLOWED_LICENSES:
        return "disallowed"
    if name in PERMISSIVE_LICENSES:
        return "permissive"
    return "unknown"


# ── Known-vulnerable dependency table (deterministic CVE stand-in) ──
# A small fixture table standing in for a live CVE feed, so the
# dependency-scan gate is fully exercisable without live network
# access to a CVE database (mirrors QA's deterministic coverage/perf
# estimators — see services/qa/workers/unit.py, performance.py).
KNOWN_VULNERABLE_PACKAGES: Dict[str, Dict[str, str]] = {
    "pyyaml":  {"cve_id": "CVE-2020-1747",  "severity": "critical", "max_safe_version": "5.3.1"},
    "requests": {"cve_id": "CVE-2018-18074", "severity": "medium",   "max_safe_version": "2.20.0"},
    "django":  {"cve_id": "CVE-2022-34265", "severity": "critical", "max_safe_version": "3.2.15"},
    "lodash":  {"cve_id": "CVE-2020-8203",  "severity": "high",     "max_safe_version": "4.17.19"},
    "log4j":   {"cve_id": "CVE-2021-44228", "severity": "critical", "max_safe_version": "2.17.0"},
    "minimist": {"cve_id": "CVE-2020-7598", "severity": "medium",   "max_safe_version": "1.2.3"},
}
