"""
services/integration/plugin_sdk/sandbox/sandbox_ipc.py
=================================
M5.1 §"IPC" — "Avoid arbitrary pickle transport. Prefer JSON-
serializable models." The parent process writes a `SandboxRequest` to
a temp file, spawns the child (`sandbox_runner.py`) with that file's
path plus a second (not-yet-existing) result-file path as argv, and
reads a `SandboxResult` back from the result file after the child
exits. Both directions are plain JSON via each model's own
`model_dump_json()`/`model_validate_json()` — no `pickle`, no
`eval`/`exec` of transported data, matching this repository's own
established copyright/security posture (see `SECURITY_REVIEW.md`'s
confirmed-zero `pickle` finding — this module keeps that true).

Malformed-payload handling: `read_result` never raises `pydantic.
ValidationError`/`json.JSONDecodeError` up to its caller — a result
file that doesn't parse (child crashed mid-write, disk full, etc.) is
reported as a `SandboxResult` with `crashed=True` and a `violations`
entry of kind `MALFORMED_PAYLOAD`, so a transport-layer failure looks
like any other sandboxed-execution failure to `SandboxManager`, not a
different exception type callers must special-case.
"""
from __future__ import annotations

import json
import os
import tempfile
import uuid
from typing import Tuple

from services.integration.plugin_sdk.sandbox.sandbox_models import (
    SandboxRequest, SandboxResult, ViolationKind, SandboxViolation,
)


def make_ipc_paths(tmp_dir: str = None) -> Tuple[str, str]:
    """Returns `(request_path, result_path)` — two sibling paths under
    a fresh temp directory, one per invocation, so concurrent sandboxed
    executions never share a request/result file."""
    base = tmp_dir or tempfile.mkdtemp(prefix="aasc_sandbox_ipc_")
    token = uuid.uuid4().hex
    return os.path.join(base, f"{token}.request.json"), os.path.join(base, f"{token}.result.json")


def write_request(path: str, request: SandboxRequest) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(request.model_dump_json())


def read_request(path: str) -> SandboxRequest:
    with open(path, "r", encoding="utf-8") as f:
        return SandboxRequest.model_validate_json(f.read())


def write_result(path: str, result: SandboxResult) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(result.model_dump_json())


def read_result(path: str, plugin_id: str, hook: str) -> SandboxResult:
    """Never raises. A missing/malformed result file (the child
    crashed before writing one, or wrote something that doesn't parse)
    produces a `crashed=True` result with a `MALFORMED_PAYLOAD`
    violation rather than propagating a parse exception."""
    if not os.path.isfile(path):
        return SandboxResult(
            plugin_id=plugin_id, hook=hook, success=False, crashed=True,
            error="sandbox result file was not written — the child process likely crashed before completing",
            violations=[SandboxViolation(plugin_id=plugin_id, kind=ViolationKind.MALFORMED_PAYLOAD,
                                          detail="result file missing")],
        )
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read()
        return SandboxResult.model_validate_json(raw)
    except (json.JSONDecodeError, ValueError) as e:
        return SandboxResult(
            plugin_id=plugin_id, hook=hook, success=False, crashed=True,
            error=f"sandbox result file was malformed: {e}",
            violations=[SandboxViolation(plugin_id=plugin_id, kind=ViolationKind.MALFORMED_PAYLOAD,
                                          detail=str(e))],
        )


def cleanup_ipc_paths(*paths: str) -> None:
    """Removes each path if present, and its parent directory if it
    ends up empty — swallows any OS error (already-removed, permission
    quirk) since cleanup is best-effort and must never raise into a
    `finally` block. Named deliberately in parallel with `plugin_loader.
    cleanup_extracted` (the equivalent cleanup function for zip-bundle
    temp dirs — see `RELIABILITY_REVIEW.md` REL1) so this module
    doesn't repeat that milestone's mistake of defining a cleanup
    function nobody calls; `sandbox_runtime.ProcessSandboxRunner.run`
    calls this in a `finally` block, unconditionally.

    **Parent-directory removal is attempted for every path given, not
    only ones whose file actually existed and got removed.**
    `make_ipc_paths()` creates its temp directory via `tempfile.
    mkdtemp()` up front, before any request/result file is necessarily
    ever written (e.g. an error before the first `write_request` call)
    — an earlier version of this function only considered a path's
    parent directory when that path's *file* existed, which left the
    directory itself behind in exactly that case. Caught by this
    milestone's own test suite leaving empty `aasc_sandbox_ipc_*`
    directories in `/tmp` after a full run — fixed here rather than
    documented as a known limitation, since unlike REL1's zip-slip
    cleanup (a genuine architectural gap deferred to a future IPC
    bridge), this one was a straightforward, fully-fixable bug in
    code this same milestone wrote."""
    parent_dirs = set()
    for path in paths:
        if not path:
            continue
        parent_dirs.add(os.path.dirname(path))
        try:
            if os.path.isfile(path):
                os.remove(path)
        except OSError:
            pass
    for d in parent_dirs:
        try:
            if os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
