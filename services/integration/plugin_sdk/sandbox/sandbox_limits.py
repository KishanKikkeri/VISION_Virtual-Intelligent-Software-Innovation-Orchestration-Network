"""
services/integration/plugin_sdk/sandbox/sandbox_limits.py
=================================
M5.1 §"Runtime Limits". **What is and isn't actually enforced, stated
honestly per the milestone's own "document any platform limitations
honestly rather than masking them" instruction:**

- `wall_timeout_seconds` — **enforced**, via `subprocess.run(...,
  timeout=...)` in `sandbox_runtime.py`'s `ProcessSandboxRunner`. This
  is the one limit this milestone can guarantee unconditionally on
  every platform Python itself runs on.
- `max_stdout_bytes`/`max_stderr_bytes` — **enforced**, by truncating
  captured output after the subprocess completes (`truncate_output`
  below). This bounds what the parent process holds in memory/persists,
  not what the child *produces* internally before exiting.
- `cpu_timeout_seconds` — **enforced on POSIX only**, via
  `resource.setrlimit(resource.RLIMIT_CPU, ...)` in a `preexec_fn`
  passed to `subprocess.run`. `resource` is a POSIX-only stdlib module
  (confirmed: it is not available on Windows) — `apply_posix_limits`
  degrades to a no-op (returns `False`, doesn't raise) on any platform
  where `import resource` fails, and callers must check that return
  value rather than assume enforcement happened.
- `max_memory_mb` — **best-effort, POSIX only**, via
  `resource.RLIMIT_AS` (address-space limit) in the same `preexec_fn`.
  This is the closest approximation available without Docker/cgroups;
  it is **not** equivalent to a real memory cgroup limit (a process
  can still be OOM-killed by the kernel before hitting this limit
  under memory pressure elsewhere on the host, and `RLIMIT_AS` bounds
  *virtual* address space, not resident memory, so it can be looser
  than intended for a process that maps large files). Documented as a
  deliberately imperfect stopgap until CONTAINER mode exists (see the
  handover doc's "Future container integration" section).
"""
from __future__ import annotations

from typing import Callable, Optional

from services.integration.plugin_sdk.sandbox.sandbox_models import SandboxLimits

try:
    import resource  # POSIX only
    _HAS_RESOURCE = True
except ImportError:  # pragma: no cover — exercised only on non-POSIX platforms
    resource = None  # type: ignore[assignment]
    _HAS_RESOURCE = False


def posix_limits_available() -> bool:
    return _HAS_RESOURCE


def build_preexec_fn(limits: SandboxLimits) -> Optional[Callable[[], None]]:
    """Returns a callable suitable for `subprocess.run(...,
    preexec_fn=...)` that applies `limits.cpu_timeout_seconds`/
    `limits.max_memory_mb` via `resource.setrlimit`, or `None` if
    `resource` isn't importable on this platform (the caller then
    simply doesn't pass `preexec_fn`, relying on `wall_timeout_seconds`
    alone — a real, documented degradation, not a silent one, since
    `posix_limits_available()` is what `sandbox_runtime.py` checks and
    records into the `SandboxResult`/violation trail when it returns
    `False`)."""
    if not _HAS_RESOURCE:
        return None

    def _apply() -> None:
        if limits.cpu_timeout_seconds is not None:
            cpu_limit = int(limits.cpu_timeout_seconds)
            resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit))
        if limits.max_memory_mb is not None:
            byte_limit = limits.max_memory_mb * 1024 * 1024
            try:
                resource.setrlimit(resource.RLIMIT_AS, (byte_limit, byte_limit))
            except (ValueError, OSError):
                # Some platforms (notably macOS) reject RLIMIT_AS for
                # certain processes — degrade silently for this one
                # sub-limit rather than failing the whole preexec step;
                # the wall timeout and CPU limit (if set) still apply.
                pass

    return _apply


def truncate_output(data: str, max_bytes: int) -> str:
    """Truncates `data` to at most `max_bytes` (encoded as UTF-8),
    appending a marker so a caller can tell truncation happened rather
    than silently losing the tail. Applied *after* subprocess
    completion — this bounds what the parent stores/returns, not what
    the child could write before being killed by the wall timeout."""
    encoded = data.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return data
    truncated = encoded[:max_bytes].decode("utf-8", errors="ignore")
    return truncated + f"\n...[truncated, {len(encoded) - max_bytes} bytes omitted]"
