"""
scripts/install/verify.py
=================================
M4.10 §4 — six checks: Python, dependencies, Docker, Redis, Postgres,
NATS. **Python and dependencies are mandatory** (a wrong Python version
or a missing required package is a real `FAIL`); **Docker/Redis/
Postgres/NATS are optional infrastructure** and follow the same
graceful-degradation convention M4.9's `environment_validator.py`
established — not found on `PATH`/not reachable becomes `WARN` with a
`fix_suggestion`, never `FAIL`, because a laptop legitimately may not
run all four locally.

    python scripts/install/verify.py
"""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[2])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from services.integration.release_validation.release_validation_models import CheckStatus, InstallCheckResult, InstallReport

REQUIRED_PYTHON = (3, 11)


def check_python() -> InstallCheckResult:
    current = sys.version_info[:2]
    if current >= REQUIRED_PYTHON:
        return InstallCheckResult(name="python", status=CheckStatus.PASS,
                                   detail=f"Python {'.'.join(map(str, current))} detected")
    return InstallCheckResult(name="python", status=CheckStatus.FAIL,
                               detail=f"Python {'.'.join(map(str, current))} detected, "
                                      f"{'.'.join(map(str, REQUIRED_PYTHON))}+ required",
                               fix_suggestion=f"Install Python {'.'.join(map(str, REQUIRED_PYTHON))}+ "
                                              f"and re-run this environment's virtualenv setup")


def check_dependencies(requirements_path: str = "requirements.txt") -> InstallCheckResult:
    import importlib.util
    import os
    if not os.path.isfile(requirements_path):
        return InstallCheckResult(name="dependencies", status=CheckStatus.WARN,
                                   detail=f"{requirements_path} not found",
                                   fix_suggestion="Ensure requirements.txt exists at the repository root")
    missing = []
    with open(requirements_path, "r", encoding="utf-8") as f:
        for raw_line in f:
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name = line.split("==")[0].split(">=")[0].split("<=")[0].split("~=")[0].strip()
            module_name = name.replace("-", "_")
            if importlib.util.find_spec(module_name) is None and importlib.util.find_spec(name) is None:
                missing.append(name)
    if not missing:
        return InstallCheckResult(name="dependencies", status=CheckStatus.PASS, detail="all packages importable")
    return InstallCheckResult(name="dependencies", status=CheckStatus.FAIL,
                               detail=f"missing packages: {missing}",
                               fix_suggestion=f"pip install -r {requirements_path} --break-system-packages")


def _binary_check(name: str, binary: str, version_args=("--version",)) -> InstallCheckResult:
    path = shutil.which(binary)
    if not path:
        return InstallCheckResult(name=name, status=CheckStatus.WARN, detail=f"{binary} not found on PATH",
                                   fix_suggestion=f"Install {binary} if this environment needs it locally")
    try:
        proc = subprocess.run([binary, *version_args], capture_output=True, text=True, timeout=5)
        version = (proc.stdout or proc.stderr).strip().splitlines()[0] if (proc.stdout or proc.stderr) else "unknown"
        return InstallCheckResult(name=name, status=CheckStatus.PASS, detail=version)
    except Exception as e:  # noqa: BLE001
        return InstallCheckResult(name=name, status=CheckStatus.WARN, detail=f"{binary} present but errored: {e}",
                                   fix_suggestion=f"Re-install or repair the {binary} installation")


def check_docker() -> InstallCheckResult:
    return _binary_check("docker", "docker")


def _tcp_check(name: str, host: str, port: int, install_hint: str, timeout: float = 1.0) -> InstallCheckResult:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return InstallCheckResult(name=name, status=CheckStatus.PASS, detail=f"{host}:{port} reachable")
    except Exception as e:  # noqa: BLE001
        return InstallCheckResult(name=name, status=CheckStatus.WARN, detail=f"{host}:{port} unreachable: {e}",
                                   fix_suggestion=install_hint)


def check_redis(host: str = "localhost", port: int = 6379) -> InstallCheckResult:
    return _tcp_check("redis", host, port, "Start Redis locally (e.g. `docker run -p 6379:6379 redis`) "
                                            "or configure REDIS_URL to point at a remote instance")


def check_postgres(host: str = "localhost", port: int = 5432) -> InstallCheckResult:
    return _tcp_check("postgres", host, port, "Start Postgres locally (e.g. `docker run -p 5432:5432 postgres`) "
                                               "or configure DATABASE_URL to point at a remote instance")


def check_nats(host: str = "localhost", port: int = 4222) -> InstallCheckResult:
    return _tcp_check("nats", host, port, "Start NATS locally (e.g. `docker run -p 4222:4222 nats`) "
                                           "or configure NATS_URL to point at a remote instance")


def run_all_checks(requirements_path: str = "requirements.txt", redis_host: str = "localhost",
                    redis_port: int = 6379, postgres_host: str = "localhost", postgres_port: int = 5432,
                    nats_host: str = "localhost", nats_port: int = 4222) -> InstallReport:
    checks = [
        check_python(),
        check_dependencies(requirements_path),
        check_docker(),
        check_redis(redis_host, redis_port),
        check_postgres(postgres_host, postgres_port),
        check_nats(nats_host, nats_port),
    ]
    return InstallReport(checks=checks)


if __name__ == "__main__":
    report = run_all_checks()
    for c in report.checks:
        print(f"  [{c.status.value}] {c.name}: {c.detail}")
        if c.fix_suggestion and c.status != CheckStatus.PASS:
            print(f"      fix: {c.fix_suggestion}")
    sys.exit(0 if report.ready else 1)
