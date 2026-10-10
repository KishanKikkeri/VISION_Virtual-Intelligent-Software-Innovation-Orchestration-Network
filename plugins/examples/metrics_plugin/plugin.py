"""
plugins/examples/metrics_plugin/plugin.py
=================================
M4.10 §6 — demonstrates the `read` permission (declared in
manifest.json, enforced by `plugin_sdk.plugin_validator`/
`plugin_runtime.context_for_plugin`, not by this module) plus a
`dashboard_section` hook that surfaces plugin-local state back to the
platform dashboard, per HookType.DASHBOARD_SECTION.

State is a plain module-level dict rather than a database — this
sample plugin is intentionally in-memory-only so it has no external
dependency to configure to run.
"""
from __future__ import annotations

_counters = {"agent_runs_started": 0, "agent_runs_finished": 0}


def before_agent_run(ctx, **kwargs):
    _counters["agent_runs_started"] += 1
    return {"count": _counters["agent_runs_started"]}


def after_agent_run(ctx, **kwargs):
    _counters["agent_runs_finished"] += 1
    return {"count": _counters["agent_runs_finished"]}


def dashboard_section(ctx, **kwargs):
    return {
        "title": "Metrics Plugin",
        "agent_runs_started": _counters["agent_runs_started"],
        "agent_runs_finished": _counters["agent_runs_finished"],
    }
