"""
services/integration/plugin_sdk/sandbox/
=================================
M5.1 — Plugin Sandboxing & Secure Execution Framework. See
docs/M5.1_Plugin_Sandboxing_Handover.md for the full writeup.

Target architecture (brief, verbatim):

    PluginRuntime -> SandboxManager -> Sandbox Runtime -> Plugin

`PluginRuntime`/`plugin_runtime.py` (M4.7) is **not modified** by this
milestone — see `sandbox_runtime.dispatch_hook_sandboxed` for the
additive, drop-in-alternative entry point a caller can use instead of
`plugin_runtime.dispatch_hook` when it wants sandboxing; existing
callers that keep using `dispatch_hook` directly are completely
unaffected, and existing plugins require zero code changes in either
case.

    sandbox_models.py    Pure Pydantic shapes: ExecutionPolicy, SandboxPolicy, SandboxLimits,
                        SandboxRequest/Result/Log/Violation (IPC + monitoring + persistence records).
    sandbox_policy.py     Manifest permissions -> Permission (reused from plugin_models, not
                        redefined) -> SandboxPolicy, default-deny.
    sandbox_limits.py     Wall/CPU timeout + stdout/stderr/memory limits — honest about what's
                        actually enforced (POSIX `resource` module) vs. best-effort.
    sandbox_context.py    Permission-enforcing ctx wrapper for both NONE (reuses
                        plugin_runtime.context_for_plugin) and PROCESS (SandboxContextProxy,
                        raises SandboxPermissionError on denied access) modes.
    sandbox_ipc.py         JSON request/result file protocol between parent and child process.
    sandbox_runner.py      The child-process entrypoint (`python -m ...sandbox_runner <req> <res>`).
    sandbox_runtime.py     SandboxManager + the two runners (NoneSandboxRunner, ProcessSandboxRunner)
                        + dispatch_hook_sandboxed.
    sandbox_monitor.py     In-memory execution counters -> SandboxDashboardSummary.
    sandbox_repository.py  Additive persistence (SandboxExecution/Policy/Violation tables).
    sandbox_export.py      json/markdown/html export, reusing M4.9's generic renderer.
    sandbox_cli.py         python sandbox_cli.py sandbox [status|run|policies|violations|history]
"""
