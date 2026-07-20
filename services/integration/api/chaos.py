"""
services/integration/api/chaos.py
=================================
M4.5 §10 "API" section:

    POST /platform/chaos/run
    GET  /platform/chaos/runs
    GET  /platform/chaos/{run_id}
    GET  /platform/chaos/report/{run_id}
    GET  /platform/chaos/scenarios
    POST /platform/chaos/scenarios/{scenario}/execute

Own `APIRouter`, same "new endpoints, no rewrites" convention M4.3's
`api/dashboard.py` and M4.4's `api/benchmarking.py` both established.
"All endpoints must be read-only except explicit execution requests"
(brief) — `POST /run` and `POST /scenarios/{scenario}/execute` are the
only two that mutate anything (both append to history), every `GET`
here is a pure read.

**Scenario execution over HTTP, and the same honest limit M4.4's
`POST /platform/benchmarks/run` documented:** this router has no
import-time dependency on any real department workflow (per
`scenario_runner.py`'s isolation principle), so the only scenario this
router can *execute* directly is the built-in `self_check` catalog
entry — a genuine fault-injection run (it really wraps a callable with
`fault_injector.apply_fault` and validates recovery), just against a
synthetic workflow stand-in rather than a real one. `POST /run` also
accepts an already-computed `ChaosRun` payload (produced out-of-band
by a process that has real department callables importable) for
persisting a real run — same two-path shape M4.4 used. See
`docs/M4.5_Chaos_Testing_Handover.md` §3 for what real integration
needs.

Process-local history (`_recent_runs`) is this router's fallback when
no `db_factory` is configured — same "process-local now, DB-backed
once wired" convention `benchmark_registry.default_registry` set for
M4.4, kept here rather than as a separate `chaos_registry.py` module
since the brief's §3 package structure list for this milestone doesn't
include one.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import structlog
from fastapi import APIRouter, Body, HTTPException, Request

from services.integration.chaos import chaos_report, fault_injector, recovery_validator, resilience_analyzer, scenario_runner
from services.integration.chaos.chaos_models import ChaosRun, FaultSpec, FaultType, ScenarioCatalogEntry

log = structlog.get_logger(__name__)
router = APIRouter(prefix="/platform/chaos", tags=["Chaos Testing"])

# Process-local fallback history — see module docstring.
_recent_runs: Dict[str, List[ChaosRun]] = {}
_runs_by_id: Dict[str, ChaosRun] = {}

_CATALOG: List[ScenarioCatalogEntry] = [
    ScenarioCatalogEntry(
        name="self_check",
        description="Built-in framework smoke test: injects a deterministic LLM_RATE_LIMITING fault "
                    "(fails the first call, succeeds thereafter) against a synthetic workflow stand-in and "
                    "validates that a single retry recovers it. Not a real workflow/component measurement — "
                    "see this router's module docstring.",
        fault_types=[FaultType.LLM_RATE_LIMITING],
        requires_external_infra=[],
    ),
]


def _infra(request: Request) -> Dict[str, Any]:
    return {"db_factory": getattr(request.app.state, "db_factory", None)}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _self_check_run(version: Optional[str] = None) -> ChaosRun:
    """The one scenario this router can execute without a real
    department callable — see module docstring. Deterministic:
    `LLM_RATE_LIMITING` fails exactly the first call (its default
    `fail_count=1`) and succeeds on every call after, so with
    `max_retries=1` this always ends in `success=True`/`recovered=True`
    — a genuine (if narrow) resilience check, not a random pass/fail.
    (`REPOSITORY_FAILURE`/other `inject_unavailable`-family faults
    decide `triggered` once at wrap time and then fail *every* call,
    so they wouldn't demonstrate retry-recovery here — see
    `fault_injector.inject_unavailable`'s docstring.)"""
    calls = {"n": 0}

    async def fake_workflow() -> Dict[str, str]:
        calls["n"] += 1
        await asyncio.sleep(0.001)
        return {"status": "ok"}

    spec = FaultSpec(fault_type=FaultType.LLM_RATE_LIMITING, target="self_check_llm", probability=1.0)

    async def rollback_check() -> bool:
        return True

    result = await scenario_runner.run_scenario(
        scenario_name="self_check", workflow_fn=fake_workflow, faults=[spec], max_retries=1,
        recovery_checks={"rollback": rollback_check},
    )

    metrics = resilience_analyzer.compute_resilience_metrics([result])
    return ChaosRun(
        name="default", version=version or _now_iso(), timestamp=_now_iso(), environment="self-check",
        scenarios=[result], metrics=metrics,
    )


@router.get("/scenarios")
async def list_scenarios() -> Dict[str, Any]:
    return {"scenarios": [s.model_dump(mode="json") for s in _CATALOG]}


@router.post("/scenarios/{scenario}/execute")
async def execute_scenario(scenario: str, request: Request) -> Dict[str, Any]:
    if scenario != "self_check":
        raise HTTPException(
            status_code=404,
            detail=f"scenario {scenario!r} is not in the built-in catalog (only 'self_check' is executable "
                   "without a real --targets-equivalent wiring — see module docstring).",
        )
    run = await _self_check_run()
    return await _persist_and_respond(request, run)


@router.post("/run")
async def run_chaos(request: Request, run: Optional[Dict[str, Any]] = Body(default=None)) -> Dict[str, Any]:
    """See module docstring's execution-path note. `run`, if given,
    must be a `ChaosRun`-shaped JSON body; omitted, this executes the
    built-in `self_check` scenario."""
    if run is not None:
        try:
            chaos_run = ChaosRun.model_validate(run)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=422, detail=f"invalid ChaosRun payload: {e}")
    else:
        chaos_run = await _self_check_run()

    return await _persist_and_respond(request, chaos_run)


async def _persist_and_respond(request: Request, run: ChaosRun) -> Dict[str, Any]:
    _recent_runs.setdefault(run.name, []).append(run)

    report = chaos_report.build_report(run)
    db_factory = _infra(request).get("db_factory")
    persisted = False
    run_id: Optional[str] = None
    if db_factory is not None:
        try:
            from services.integration.chaos.chaos_repository import ChaosRepository
            async with db_factory() as db:
                run_id = await ChaosRepository.record_run(
                    db, run, resilience_score=report.resilience_score, recommendations=report.recommendations,
                    summary=report.summary,
                )
            persisted = True
        except Exception as e:  # noqa: BLE001
            log.warning("chaos_run_persist_failed", name=run.name, error=str(e))

    if run_id is None:
        run_id = f"inmemory:{run.name}:{len(_recent_runs[run.name]) - 1}"
    _runs_by_id[run_id] = run

    return {"persisted": persisted, "run_id": run_id, "run": run.model_dump(mode="json"),
            "resilience_score": report.resilience_score}


@router.get("/runs")
async def list_runs(request: Request, name: str = "default", limit: int = 50) -> Dict[str, Any]:
    db_factory = _infra(request).get("db_factory")
    if db_factory is not None:
        try:
            from services.integration.chaos.chaos_repository import ChaosRepository
            async with db_factory() as db:
                records = await ChaosRepository.list_history(db, name, limit=limit)
            if records:
                return {"name": name, "count": len(records), "runs": [r.run.model_dump(mode="json") for r in records],
                        "source": "database"}
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read chaos registry: {e}")

    runs = _recent_runs.get(name, [])[-limit:]
    return {"name": name, "count": len(runs), "runs": [r.model_dump(mode="json") for r in runs], "source": "in-memory"}


@router.get("/{run_id}")
async def get_run(run_id: str, request: Request) -> Dict[str, Any]:
    db_factory = _infra(request).get("db_factory")
    if db_factory is not None:
        try:
            from services.integration.chaos.chaos_repository import ChaosRepository
            async with db_factory() as db:
                record = await ChaosRepository.get_by_id(db, run_id)
            if record is not None:
                return record.run.model_dump(mode="json")
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read chaos registry: {e}")

    run = _runs_by_id.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no chaos run found with id {run_id!r}")
    return run.model_dump(mode="json")


@router.get("/report/{run_id}")
async def get_report(run_id: str, request: Request, format: str = "markdown") -> Any:
    db_factory = _infra(request).get("db_factory")
    run: Optional[ChaosRun] = None
    if db_factory is not None:
        try:
            from services.integration.chaos.chaos_repository import ChaosRepository
            async with db_factory() as db:
                record = await ChaosRepository.get_by_id(db, run_id)
            if record is not None:
                run = record.run
        except Exception as e:  # noqa: BLE001
            raise HTTPException(status_code=503, detail=f"could not read chaos registry: {e}")

    if run is None:
        run = _runs_by_id.get(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"no chaos run found with id {run_id!r}")

    report = chaos_report.build_report(run)
    if format == "json":
        return report.model_dump(mode="json")
    if format == "html":
        from fastapi.responses import HTMLResponse
        return HTMLResponse(chaos_report.render_html(report))
    if format == "markdown":
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(chaos_report.render_markdown(report))
    raise HTTPException(status_code=400, detail=f"unknown format {format!r}; choose markdown|json|html")
