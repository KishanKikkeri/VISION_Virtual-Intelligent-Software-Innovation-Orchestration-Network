"""
services/integration/plugin_sdk/sandbox/sandbox_runtime.py
=================================
M5.1 §"Sandbox Manager" / §"Architectural Rule" — the module
implementing:

    PluginRuntime -> SandboxManager -> Sandbox Runtime -> Plugin

`SandboxManager.execute` is the one function that "chooses execution
mode, executes plugin, captures output, returns PluginResult" (brief's
pseudo-flow, verbatim) — a caller (see `dispatch_hook_sandboxed` below)
never needs to know whether a given plugin ran in-process or in a
subprocess.

**`plugin_runtime.py` is not modified by this module or anywhere else
in this milestone.** `dispatch_hook_sandboxed` here is an additive,
parallel entry point with the same `List[HookExecutionResult]` return
shape `plugin_runtime.dispatch_hook` already has, so a caller can
choose between the two without any change to how it consumes the
result. Existing callers that keep calling `plugin_runtime.dispatch_
hook` directly are completely unaffected — that function's own source
file has zero lines changed by M5.1.
"""
from __future__ import annotations

import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

from services.integration.plugin_sdk.plugin_models import HookExecutionResult, HookType, PluginRecord
from services.integration.plugin_sdk.plugin_runtime import PluginContext
from services.integration.plugin_sdk.sandbox import sandbox_ipc, sandbox_monitor
from services.integration.plugin_sdk.sandbox.sandbox_context import build_context_payload, build_none_mode_context
from services.integration.plugin_sdk.sandbox.sandbox_limits import build_preexec_fn, truncate_output
from services.integration.plugin_sdk.sandbox.sandbox_models import (
    ExecutionPolicy, SandboxPolicy, SandboxRequest, SandboxResult, ViolationKind, SandboxViolation,
)
from services.integration.plugin_sdk.sandbox.sandbox_policy import build_policy


class NoneSandboxRunner:
    """§"Execution Modes", `NONE` — runs the plugin's hook function
    in-process, exactly as `plugin_runtime.dispatch_hook` already does
    for a single plugin. This is what makes "existing plugins must
    continue to run without modification" true: a policy with
    `execution_mode=NONE` (the default `SandboxPolicy()` — see
    `sandbox_models.py`) produces identical behavior to calling
    `plugin_runtime.dispatch_hook` directly, because it *is* that same
    code path, not a re-implementation of it."""

    def run(self, record: PluginRecord, module: Any, hook: HookType, base_context: PluginContext,
            policy: SandboxPolicy, **kwargs: Any) -> SandboxResult:
        start = time.monotonic()
        ctx = build_none_mode_context(base_context, record.manifest.permissions)
        hook_fn = getattr(module, hook.value, None)
        if hook_fn is None:
            return SandboxResult(plugin_id=record.manifest.id, hook=hook.value, success=True, result=None,
                                  duration_ms=round((time.monotonic() - start) * 1000, 2))
        try:
            result_value = hook_fn(ctx, **kwargs)
            return SandboxResult(plugin_id=record.manifest.id, hook=hook.value, success=True, result=result_value,
                                  duration_ms=round((time.monotonic() - start) * 1000, 2))
        except Exception as e:  # noqa: BLE001 — one plugin's bug must never crash dispatch, matching plugin_runtime.dispatch_hook
            return SandboxResult(plugin_id=record.manifest.id, hook=hook.value, success=False,
                                  error=f"{type(e).__name__}: {e}",
                                  duration_ms=round((time.monotonic() - start) * 1000, 2))


class ProcessSandboxRunner:
    """§"PROCESS Mode" — spawns `sandbox_runner.py` as a fresh
    interpreter, waits up to `policy.limits.wall_timeout_seconds`,
    and always cleans up its IPC temp files in a `finally` block
    (deliberately unlike `plugin_loader`'s zip-extraction temp dirs
    before the M5.1-adjacent audit fix — see `sandbox_ipc.
    cleanup_ipc_paths`'s docstring for the explicit cross-reference).
    """

    def __init__(self, plugins_dir: str):
        self.plugins_dir = plugins_dir

    def run(self, record: PluginRecord, hook: HookType, base_context: PluginContext, policy: SandboxPolicy,
            **kwargs: Any) -> SandboxResult:
        plugin_id = record.manifest.id
        req_path, res_path = sandbox_ipc.make_ipc_paths()
        try:
            request = SandboxRequest(
                plugin_id=plugin_id, hook=hook.value, plugins_dir=self.plugins_dir, kwargs=kwargs,
                context=build_context_payload(base_context, policy), limits=policy.limits,
            )
            sandbox_ipc.write_request(req_path, request)

            preexec_fn = build_preexec_fn(policy.limits)
            violations: List[SandboxViolation] = []
            if preexec_fn is None:
                violations.append(SandboxViolation(
                    plugin_id=plugin_id, kind=ViolationKind.LIMIT_EXCEEDED,
                    detail="CPU/memory limits not enforced on this platform (POSIX `resource` module unavailable)"))

            start = time.monotonic()
            try:
                proc = subprocess.run(
                    [sys.executable, "-m", "services.integration.plugin_sdk.sandbox.sandbox_runner",
                     req_path, res_path],
                    capture_output=True, text=True, timeout=policy.limits.wall_timeout_seconds,
                    preexec_fn=preexec_fn if os.name == "posix" else None,
                )
            except subprocess.TimeoutExpired as e:
                duration = round((time.monotonic() - start) * 1000, 2)
                return SandboxResult(
                    plugin_id=plugin_id, hook=hook.value, success=False, timed_out=True,
                    error=f"execution exceeded wall_timeout_seconds={policy.limits.wall_timeout_seconds}",
                    duration_ms=duration,
                    stdout=truncate_output(e.stdout.decode("utf-8", "replace") if isinstance(e.stdout, bytes)
                                            else (e.stdout or ""), policy.limits.max_stdout_bytes),
                    stderr=truncate_output(e.stderr.decode("utf-8", "replace") if isinstance(e.stderr, bytes)
                                            else (e.stderr or ""), policy.limits.max_stderr_bytes),
                    violations=violations + [SandboxViolation(plugin_id=plugin_id, kind=ViolationKind.TIMEOUT,
                                                               detail="wall timeout exceeded")],
                )

            duration = round((time.monotonic() - start) * 1000, 2)
            stdout = truncate_output(proc.stdout or "", policy.limits.max_stdout_bytes)
            stderr = truncate_output(proc.stderr or "", policy.limits.max_stderr_bytes)

            if proc.returncode != 0:
                return SandboxResult(
                    plugin_id=plugin_id, hook=hook.value, success=False, crashed=True,
                    error=f"sandbox child process exited with code {proc.returncode} "
                          f"(likely killed by a resource limit, e.g. RLIMIT_CPU/RLIMIT_AS)",
                    duration_ms=duration, stdout=stdout, stderr=stderr,
                    violations=violations + [SandboxViolation(plugin_id=plugin_id, kind=ViolationKind.CRASH,
                                                               detail=f"exit code {proc.returncode}")],
                )

            result = sandbox_ipc.read_result(res_path, plugin_id, hook.value)
            result.duration_ms = duration
            result.stdout = stdout
            result.stderr = stderr
            result.violations = violations + result.violations
            return result
        finally:
            sandbox_ipc.cleanup_ipc_paths(req_path, res_path)


class SandboxManager:
    """§"Sandbox Manager". `execute` is the single dispatch point:
    picks `NoneSandboxRunner`/`ProcessSandboxRunner` by
    `policy.execution_mode`, runs it, records the outcome into
    `sandbox_monitor` (§"Monitoring"), and returns a `SandboxResult`.
    Adding a future mode (`CONTAINER`/`FIRECRACKER`/`WASM` — design
    only per the brief) means adding one more `elif` here and one new
    runner class; nothing about this method's shape needs to change."""

    def __init__(self, plugins_dir: str, monitor: Optional[sandbox_monitor.SandboxMonitor] = None):
        self.plugins_dir = plugins_dir
        self.monitor = monitor or sandbox_monitor.SandboxMonitor()
        self._none_runner = NoneSandboxRunner()
        self._process_runner = ProcessSandboxRunner(plugins_dir)

    def execute(self, record: PluginRecord, module: Any, hook: HookType, base_context: PluginContext,
                policy: SandboxPolicy, **kwargs: Any) -> SandboxResult:
        if policy.execution_mode == ExecutionPolicy.PROCESS:
            result = self._process_runner.run(record, hook, base_context, policy, **kwargs)
        else:
            result = self._none_runner.run(record, module, hook, base_context, policy, **kwargs)
        self.monitor.record(policy.execution_mode, result)
        return result


def dispatch_hook_sandboxed(
    hook: HookType, records: List[PluginRecord], modules: Dict[str, Any], base_context: PluginContext,
    plugins_dir: str, policies: Optional[Dict[str, SandboxPolicy]] = None,
    monitor: Optional[sandbox_monitor.SandboxMonitor] = None, **kwargs: Any,
) -> List[HookExecutionResult]:
    """The additive, drop-in-alternative to `plugin_runtime.dispatch_
    hook` this milestone's brief asks for ("PluginRuntime should
    delegate execution... PluginRuntime should not know how the
    sandbox works internally"). Same signature shape as `dispatch_
    hook` plus `plugins_dir`/`policies`/`monitor`; same sorted-by-
    plugin-id iteration order; same per-plugin failure isolation.
    `policies` defaults to `ExecutionPolicy.NONE` (via `build_policy`)
    for any plugin not explicitly given one — i.e. calling this with
    no `policies` argument at all reproduces `plugin_runtime.dispatch_
    hook`'s exact behavior, just routed through `SandboxManager`
    instead of calling the hook function directly."""
    policies = policies or {}
    manager = SandboxManager(plugins_dir, monitor=monitor)
    results: List[HookExecutionResult] = []

    for record in sorted(records, key=lambda r: r.manifest.id):
        module = modules.get(record.manifest.id)
        if module is None:
            continue
        policy = policies.get(record.manifest.id) or build_policy(
            record.manifest.id, record.manifest.permissions, ExecutionPolicy.NONE)

        sandbox_result = manager.execute(record, module, hook, base_context, policy, **kwargs)
        results.append(HookExecutionResult(
            plugin_id=record.manifest.id, hook=hook, success=sandbox_result.success,
            result=sandbox_result.result, error=sandbox_result.error,
            duration_ms=sandbox_result.duration_ms,
        ))
    return results
