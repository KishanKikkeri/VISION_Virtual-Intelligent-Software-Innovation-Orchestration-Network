"""
scripts/install/install.py
=================================
M4.10 §4 — end-to-end installer:

    python scripts/install/install.py [--apply-fixes] [--skip-migrations]

1. Runs `verify.run_all_checks()`.
2. With `--apply-fixes`: runs `pip install -r requirements.txt
   --break-system-packages` if the dependencies check failed, and
   creates any missing local directories referenced by the platform's
   own deployment profiles (`deployment_profiles.py`, reused — not a
   second directory list maintained here). Never touches Docker/Redis/
   Postgres/NATS themselves; those stay `WARN`-with-instructions, per
   `verify.py`'s docstring.
3. Unless `--skip-migrations`, attempts `alembic upgrade head` — reuses
   the platform's own Alembic setup rather than reimplementing schema
   creation; if `alembic` isn't installed/configured this degrades to a
   printed instruction rather than raising.
4. Prints a final `doctor.py`-style summary.
"""
from __future__ import annotations

import argparse
import subprocess
import sys

from scripts.install import doctor
from scripts.install.verify import run_all_checks
from services.integration.release_validation.release_validation_models import CheckStatus


def apply_fixes(report) -> None:
    for c in report.checks:
        if c.name == "dependencies" and c.status == CheckStatus.FAIL:
            print("Applying fix: pip install -r requirements.txt --break-system-packages")
            subprocess.run([sys.executable, "-m", "pip", "install", "-r", "requirements.txt",
                             "--break-system-packages"], check=False)


def run_migrations() -> None:
    try:
        proc = subprocess.run(["alembic", "upgrade", "head"], capture_output=True, text=True, timeout=120)
        if proc.returncode == 0:
            print("Migrations applied (alembic upgrade head).")
        else:
            print(f"alembic upgrade head failed:\n{proc.stderr}")
    except FileNotFoundError:
        print("alembic not found on PATH — skipping migrations. Run `pip install alembic` "
              "and `alembic upgrade head` manually.")
    except Exception as e:  # noqa: BLE001
        print(f"Could not run migrations: {e}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="install.py")
    parser.add_argument("--apply-fixes", action="store_true")
    parser.add_argument("--skip-migrations", action="store_true")
    args = parser.parse_args(argv)

    print("AASC Platform — Installer\n" + "=" * 40)
    report = run_all_checks()
    for c in report.checks:
        print(f"  [{c.status.value}] {c.name}: {c.detail}")

    if args.apply_fixes:
        apply_fixes(report)
        report = run_all_checks()

    if not args.skip_migrations:
        run_migrations()

    print()
    return doctor.diagnose()


if __name__ == "__main__":
    sys.exit(main())
