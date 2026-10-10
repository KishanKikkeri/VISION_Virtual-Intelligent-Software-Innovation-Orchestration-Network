"""
scripts/install/doctor.py
=================================
M4.10 §4 — `python scripts/install/doctor.py` runs `verify.run_all_checks()`
and prints a human-focused diagnosis: what's wrong and the exact fix for
each failing/warning check. Doesn't re-implement any check itself —
`verify.py` owns the checks, this module owns presentation, same split
`production_cli.py cmd_check` uses versus `environment_validator.py`.
"""
import sys
from pathlib import Path

repo_root = str(Path(__file__).resolve().parents[2])
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from scripts.install.verify import run_all_checks
from services.integration.release_validation.release_validation_models import CheckStatus


def diagnose() -> int:
    report = run_all_checks()
    print("AASC Platform — Installation Doctor")
    print("=" * 40)
    ok = [c for c in report.checks if c.status == CheckStatus.PASS]
    problems = [c for c in report.checks if c.status != CheckStatus.PASS]

    print(f"\n{len(ok)}/{len(report.checks)} checks healthy.\n")

    if not problems:
        print("Everything looks good — the platform is ready to run.")
        return 0

    print("Issues found:\n")
    for c in problems:
        print(f"  [{c.status.value.upper()}] {c.name}")
        print(f"    {c.detail}")
        if c.fix_suggestion:
            print(f"    -> {c.fix_suggestion}")
        print()

    hard_failures = [c for c in problems if c.status == CheckStatus.FAIL]
    if hard_failures:
        print(f"{len(hard_failures)} check(s) must be fixed before the platform will run correctly.")
        return 1
    print("All remaining issues are optional local infrastructure (WARN) — "
          "the platform can still run in degraded/local mode.")
    return 0


if __name__ == "__main__":
    sys.exit(diagnose())
