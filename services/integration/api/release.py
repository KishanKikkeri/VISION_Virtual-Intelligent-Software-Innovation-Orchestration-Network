"""
services/integration/api/release.py
=================================
M4.10 §"APIs" — exposes this milestone's tooling under `/platform/
release/*`:

    GET  /platform/release/status
    POST /platform/release/check
    POST /platform/release/docs/generate
    GET  /platform/release/docs
    GET  /platform/release/install-check
    POST /platform/release/benchmark
    GET  /platform/release/benchmark
    POST /platform/release/qa
    GET  /platform/release/qa
    POST /platform/release/package
    GET  /platform/release/manifest

Same DB-preferred-with-in-memory-fallback convention M4.9's
`api/production.py` uses: reports are cached on `request.app.state`
process-local lists when no `db_factory` is configured, rather than
requiring a database for this milestone's endpoints to function.
Nothing here writes to the platform's SQL schema — this package is
filesystem/report-oriented, per §"Database: Additive only" (no new
tables were needed for this milestone; see the M4.10 handover doc §2
for that decision).
"""
from __future__ import annotations

from typing import Any, Dict, List

from fastapi import APIRouter, Body, HTTPException, Query, Request

from services.integration.release_validation import (
    documentation_generator, final_qa, release_export, release_packaging,
)
from services.integration.release_validation.readiness_report import build_readiness_report

router = APIRouter(prefix="/platform/release", tags=["Release Validation"])


def _history(request: Request, attr: str) -> List[Any]:
    if not hasattr(request.app.state, attr):
        setattr(request.app.state, attr, [])
    return getattr(request.app.state, attr)


@router.get("/status")
async def get_status(request: Request) -> Dict[str, Any]:
    """Cheapest possible endpoint: the most recent readiness report this
    process has computed, or a 404-shaped hint to call `/check` first —
    never triggers a fresh (potentially slow) check itself."""
    history = _history(request, "release_readiness_history")
    if not history:
        return {"available": False, "detail": "no readiness report computed yet in this process; POST /check first"}
    latest = history[-1]
    return {"available": True, "report": latest.model_dump(mode="json")}


@router.post("/check")
async def post_check(request: Request, version: str = Body(..., embed=True),
                      environment: str = Body("production", embed=True)) -> Dict[str, Any]:
    report = build_readiness_report(version, environment=environment)
    _history(request, "release_readiness_history").append(report)
    return report.model_dump(mode="json")


@router.post("/docs/generate")
async def post_docs_generate(request: Request, out_dir: str = Body("docs/generated", embed=True)) -> Dict[str, Any]:
    written = documentation_generator.generate_all(out_dir=out_dir)
    return {"written": written, "count": len(written)}


@router.get("/docs")
async def get_docs(request: Request, docs_dir: str = Query("docs/generated")) -> Dict[str, Any]:
    from services.integration.release_validation.readiness_report import scan_documentation_completeness
    result = scan_documentation_completeness(docs_dir)
    return result.model_dump(mode="json")


@router.get("/install-check")
async def get_install_check(request: Request) -> Dict[str, Any]:
    try:
        from scripts.install.verify import run_all_checks
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"installer verification unavailable: {e}") from e
    report = run_all_checks()
    return report.model_dump(mode="json")


@router.post("/benchmark")
async def post_benchmark(request: Request) -> Dict[str, Any]:
    try:
        from benchmarks.runner import run_benchmarks
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=503, detail=f"benchmark runner unavailable: {e}") from e
    report = run_benchmarks()
    _history(request, "release_benchmark_history").append(report)
    return report.model_dump(mode="json")


@router.get("/benchmark")
async def get_benchmark(request: Request) -> Dict[str, Any]:
    history = _history(request, "release_benchmark_history")
    if not history:
        return {"available": False, "detail": "no benchmark run yet in this process; POST /benchmark first"}
    return {"available": True, "report": history[-1].model_dump(mode="json")}


@router.post("/qa")
async def post_qa(request: Request) -> Dict[str, Any]:
    report = final_qa.run_final_qa()
    _history(request, "release_qa_history").append(report)
    return report.model_dump(mode="json")


@router.get("/qa")
async def get_qa(request: Request) -> Dict[str, Any]:
    history = _history(request, "release_qa_history")
    if not history:
        return {"available": False, "detail": "no QA run yet in this process; POST /qa first"}
    return {"available": True, "report": history[-1].model_dump(mode="json")}


@router.post("/package")
async def post_package(request: Request, version: str = Body(..., embed=True)) -> Dict[str, Any]:
    manifest = release_packaging.build_release_manifest(version)
    _history(request, "release_manifest_history").append(manifest)
    return manifest.model_dump(mode="json")


@router.get("/manifest")
async def get_manifest(request: Request) -> Dict[str, Any]:
    history = _history(request, "release_manifest_history")
    if not history:
        return {"available": False, "detail": "no manifest built yet in this process; POST /package first"}
    return {"available": True, "manifest": history[-1].model_dump(mode="json")}


@router.get("/export")
async def get_export(request: Request, fmt: str = Query("markdown")) -> Dict[str, Any]:
    """Exports the most recent readiness report in the requested format
    (json/markdown/html), reusing M4.9's generic exporter."""
    history = _history(request, "release_readiness_history")
    if not history:
        raise HTTPException(status_code=404, detail="no readiness report computed yet in this process")
    try:
        content = release_export.export_report(history[-1], fmt)
    except release_export.ExportError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    return {"format": fmt, "content": content}
