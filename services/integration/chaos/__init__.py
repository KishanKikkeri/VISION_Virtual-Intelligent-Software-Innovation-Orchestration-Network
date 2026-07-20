"""
services/integration/chaos/
=================================
M4.5 — Chaos Testing & Resilience Framework. See
docs/M4.5_Chaos_Testing_Handover.md for the full writeup.

    chaos_models.py         Pure Pydantic shapes (FaultSpec, FaultEvent,
                            RecoverySignal, ScenarioResult, ResilienceMetrics,
                            ChaosRun, ChaosComparison, ChaosReport, ...).
    fault_injector.py       Wraps a real callable per fault type; never
                            patches production code.
    scenario_runner.py      Injects faults, executes, times (via M4.4's
                            benchmark_runner), validates recovery, assembles
                            a ChaosRun. Scenarios are independent by construction.
    recovery_validator.py   Pure signal-folding for §6 Recovery Validation.
    resilience_analyzer.py  Derives ResilienceMetrics + resilience_score +
                            recommendations + compare_runs (regression detection).
    chaos_repository.py     Repository-pattern DB persistence (chaos_scenarios /
                            chaos_runs / fault_events / resilience_reports).
    chaos_report.py         markdown / json / html report rendering.
    chaos_export.py         csv / json / markdown flat export (metrics or faults).
    chaos_dashboard.py      Read-only projection for the M4.3 Live Operations
                            Dashboard's chaos card — no separate UI.
"""
