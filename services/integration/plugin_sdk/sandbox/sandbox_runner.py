"""
services/integration/plugin_sdk/sandbox/sandbox_runner.py
=================================
M5.1 §"PROCESS Mode" — the module executed **inside the sandboxed
child process**:

    python -m services.integration.plugin_sdk.sandbox.sandbox_runner <request_path> <result_path>

Responsibilities, in order:

1. Read a `SandboxRequest` from `request_path` (`sandbox_ipc.
   read_request`).
2. Re-run `plugin_loader.discover_plugins`/`import_entrypoint` against
   `request.plugins_dir`/`request.plugin_id` — the child starts a
   **fresh interpreter**, so it cannot receive an already-imported
   module object from the parent; re-importing here (not passing a
   live object) is the only way a subprocess-isolated design can work
   at all, reusing M4.7's existing loader rather than a second import
   mechanism.
3. Build a `SandboxContextProxy` from `request.context` (the
   already-filtered, JSON-only payload — this module trusts the parent
   built it correctly via `sandbox_policy`/`sandbox_context`, it does
   not re-derive permissions itself).
4. Look up the hook function on the imported module by name (same
   `getattr(module, hook_name, None)` convention `plugin_runtime.
   dispatch_hook` uses — not duplicated logic, the same rule restated
   for a standalone process that doesn't call `dispatch_hook` itself).
5. Call it, catching every exception (a plugin's own bug must produce
   a `SandboxResult`, not a bare traceback the parent has to parse from
   stderr) and building a `SandboxResult`.
6. Write the result to `result_path` and **always exit 0** — a nonzero
   exit code is reserved by `sandbox_runtime.ProcessSandboxRunner` to
   mean "the process itself crashed/was killed before it could write a
   result" (e.g. hit `RLIMIT_CPU`/`RLIMIT_AS` and was SIGKILLed), which
   must be distinguishable from "the plugin's hook function raised an
   exception, which we caught and reported normally."
"""
from __future__ import annotations

import sys
import time
import traceback

from services.integration.plugin_sdk import plugin_loader
from services.integration.plugin_sdk.sandbox import sandbox_ipc
from services.integration.plugin_sdk.sandbox.sandbox_context import build_sandbox_context_proxy
from services.integration.plugin_sdk.sandbox.sandbox_models import SandboxLog, SandboxResult


def run(request_path: str, result_path: str) -> int:
    start = time.monotonic()
    try:
        request = sandbox_ipc.read_request(request_path)
    except Exception as e:  # noqa: BLE001 — a malformed request must still produce SOME result file
        sandbox_ipc.write_result(result_path, SandboxResult(
            plugin_id="unknown", hook="unknown", success=False, crashed=True,
            error=f"sandbox_runner could not read its own request file: {e}",
        ))
        return 0

    plugin_id, hook_name = request.plugin_id, request.hook
    logs = []

    try:
        discovered = plugin_loader.discover_plugins(request.plugins_dir)
        target = next((d for d in discovered if d.manifest.id == plugin_id), None)
        if target is None:
            raise plugin_loader.PluginLoadError(f"plugin {plugin_id!r} not found under {request.plugins_dir!r}")

        module = plugin_loader.import_entrypoint(target)
        hook_fn = getattr(module, hook_name, None)
        if hook_fn is None:
            sandbox_ipc.write_result(result_path, SandboxResult(
                plugin_id=plugin_id, hook=hook_name, success=True, result=None,
                duration_ms=round((time.monotonic() - start) * 1000, 2),
                logs=[SandboxLog(plugin_id=plugin_id, level="info",
                                  message=f"plugin does not implement hook {hook_name!r} — skipped")],
            ))
            return 0

        ctx = build_sandbox_context_proxy(plugin_id, request.context)
        result_value = hook_fn(ctx, **request.kwargs)

        sandbox_ipc.write_result(result_path, SandboxResult(
            plugin_id=plugin_id, hook=hook_name, success=True, result=result_value,
            duration_ms=round((time.monotonic() - start) * 1000, 2), logs=logs,
        ))
        return 0
    except Exception as e:  # noqa: BLE001 — "must never crash the platform" applies inside the sandbox too
        sandbox_ipc.write_result(result_path, SandboxResult(
            plugin_id=plugin_id, hook=hook_name, success=False, crashed=False,
            error=f"{type(e).__name__}: {e}\n{traceback.format_exc()}",
            duration_ms=round((time.monotonic() - start) * 1000, 2), logs=logs,
        ))
        return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) != 2:
        print("usage: sandbox_runner.py <request_path> <result_path>", file=sys.stderr)
        return 2
    return run(argv[0], argv[1])


if __name__ == "__main__":
    sys.exit(main())
