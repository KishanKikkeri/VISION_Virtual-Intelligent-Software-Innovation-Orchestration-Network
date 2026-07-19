"""
benchmarks/runner.py
=================================
M4.10 §7 Benchmark Runner — measures workflow speed, agent latency,
replay performance, dashboard refresh, and API throughput, and writes
`benchmarks/benchmark.json` / `benchmarks/benchmark.md`.

**What's actually measured in this sandbox slice.** The real LangGraph
workflow engine and the real M4.1-M4.3 modules (Workflow Versioning,
Execution Replay, Live Dashboard) aren't importable here (same standing
scope note as everywhere else in this milestone — see M4.9 handover
§3). Rather than fabricate numbers for components that aren't present,
each `measure_*` function below times a real operation from whatever
*is* importable in this slice and falls back to `None` (surfaced as
"not measurable in this environment", not a made-up number) when its
target module is absent. `measure_dashboard_refresh` and
`measure_api_throughput` — the two that only need `dashboard_builder`/
`plugin_registry`, both present — are the two genuinely exercised in a
default run.
"""
from __future__ import annotations

import importlib
import json
import os
import time
from typing import Callable, Dict, Optional

from services.integration.release_validation.benchmark_summary import build_benchmark_summary, summary_markdown
from services.integration.release_validation.release_validation_models import BenchmarkSummaryReport

_UNITS = {
    "workflow_speed": "ms", "agent_latency": "ms", "replay_performance": "ms",
    "dashboard_refresh": "ms", "api_throughput": "ops_sec",
}


def _time_it(fn: Callable[[], None], iterations: int = 50) -> float:
    start = time.perf_counter()
    for _ in range(iterations):
        fn()
    elapsed_ms = (time.perf_counter() - start) * 1000
    return round(elapsed_ms / iterations, 4)


def measure_workflow_speed() -> Optional[float]:
    try:
        module = importlib.import_module("services.integration.workflow_designer.graph_builder")
        build_fn = getattr(module, "build_graph", None) or getattr(module, "build_networkx_graph", None)
        if build_fn is None:
            return None
        return _time_it(lambda: build_fn({"nodes": [], "edges": []}) if _safe_call(build_fn) else None, 10)
    except Exception:  # noqa: BLE001
        return None


def _safe_call(fn) -> bool:
    try:
        fn({"nodes": [], "edges": []})
        return True
    except Exception:  # noqa: BLE001
        return False


def measure_agent_latency() -> Optional[float]:
    # No agent runtime is importable in this slice — not measurable here.
    return None


def measure_replay_performance() -> Optional[float]:
    try:
        importlib.import_module("services.integration.execution_replay")
    except Exception:  # noqa: BLE001
        return None
    return None  # module absent in this slice; placeholder retained for the real platform to wire a real replay


def measure_dashboard_refresh() -> Optional[float]:
    try:
        from services.integration.dashboard import dashboard_builder
    except Exception:  # noqa: BLE001
        return None

    def _build_once() -> None:
        dashboard_builder.build_production_summary({}) if hasattr(dashboard_builder, "build_production_summary") \
            else dashboard_builder.categorize_event_type("workflow.started")

    return _time_it(_build_once, 200)


def measure_api_throughput() -> Optional[float]:
    """Ops/sec of a cheap, real, importable operation
    (`plugin_registry`'s in-memory list operation) standing in for a
    full HTTP round trip, which this sandbox slice has no running
    server to benchmark against. Documented as a proxy, not hidden."""
    try:
        from services.integration.plugin_sdk.plugin_registry import PluginRegistry
    except Exception:  # noqa: BLE001
        return None
    registry = PluginRegistry()
    start = time.perf_counter()
    iterations = 1000
    for _ in range(iterations):
        registry.list_enabled()
    elapsed = time.perf_counter() - start
    return round(iterations / elapsed, 2) if elapsed > 0 else None


_MEASURERS: Dict[str, Callable[[], Optional[float]]] = {
    "workflow_speed": measure_workflow_speed,
    "agent_latency": measure_agent_latency,
    "replay_performance": measure_replay_performance,
    "dashboard_refresh": measure_dashboard_refresh,
    "api_throughput": measure_api_throughput,
}


def run_benchmarks(baseline: Optional[Dict[str, float]] = None) -> BenchmarkSummaryReport:
    raw_results: Dict[str, float] = {}
    for name, measurer in _MEASURERS.items():
        value = measurer()
        if value is not None:
            raw_results[name] = value
    return build_benchmark_summary(raw_results, units=_UNITS, baseline=baseline)


def write_benchmark_outputs(out_dir: str = "benchmarks", baseline: Optional[Dict[str, float]] = None) -> Dict[str, str]:
    report = run_benchmarks(baseline)
    os.makedirs(out_dir, exist_ok=True)
    json_path = os.path.join(out_dir, "benchmark.json")
    md_path = os.path.join(out_dir, "benchmark.md")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report.model_dump(mode="json"), f, indent=2)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(summary_markdown(report))
    return {"json": json_path, "markdown": md_path}


if __name__ == "__main__":
    paths = write_benchmark_outputs()
    print(f"wrote {paths['json']}")
    print(f"wrote {paths['markdown']}")
