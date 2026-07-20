"""
services/integration/production/production_cli.py
=================================
M4.9 §11 CLI:

    python production_cli.py production check --environment production
    python production_cli.py production validate <assets_dir> --environment production
    python production_cli.py production release <version> [--previous <version>]
    python production_cli.py production backup --scopes database,configuration --dest <dir>
    python production_cli.py production restore <backup_json_path> [--mode full --yes]
    python production_cli.py production config --environment production [--format yaml]
    python production_cli.py production status --environment production
    python production_cli.py production checklist --environment production

Every command that touches persisted state accepts `--db-url`
(optional). **`restore` requires an explicit `--yes` flag to move past
`dry_run`** — same confirmation-gate convention `restore_manager.
apply_restore` enforces at the module level; the CLI simply surfaces
that gate as a flag rather than working around it.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import List, Optional

from services.integration.production import (
    backup_manager, configuration_manager, deployment_profiles, deployment_validator, environment_validator,
    release_manager, restore_manager,
)
from services.integration.production.release_models import BackupRecord, BackupScope, BackupType, RestoreMode


def cmd_check(args: argparse.Namespace) -> int:
    report = environment_validator.run_environment_checks(args.environment)
    print(f"Environment {report.environment!r}: {report.overall_status.value.upper()}")
    for check in report.checks:
        print(f"  [{check.status.value}] {check.name} ({check.category}): {check.detail}")
    return 0 if report.ready else 1


def cmd_validate(args: argparse.Namespace) -> int:
    assets = {}
    if os.path.isdir(args.assets_dir):
        for fname in sorted(os.listdir(args.assets_dir)):
            path = os.path.join(args.assets_dir, fname)
            if os.path.isfile(path):
                with open(path, "r", encoding="utf-8") as f:
                    assets[fname] = f.read()
    elif os.path.isfile(args.assets_dir):
        with open(args.assets_dir, "r", encoding="utf-8") as f:
            assets[os.path.basename(args.assets_dir)] = f.read()
    else:
        print(f"{args.assets_dir!r} is not a file or directory")
        return 1

    result = deployment_validator.validate_deployment(assets, args.environment)
    print(f"Deployment validation: {'VALID' if result.valid else 'INVALID'} ({len(result.assets_checked)} asset(s))")
    for issue in result.issues:
        print(f"  [{issue.severity}] {issue.asset}: {issue.message}")
    return 0 if result.valid else 1


def cmd_release(args: argparse.Namespace) -> int:
    try:
        release = release_manager.build_release(args.version, previous_version=args.previous,
                                                  release_notes=args.notes or [])
    except ValueError as e:
        print(f"Release build failed: {e}")
        return 1
    print(f"Release {release.version} ({release.channel}) — {len(release.artifacts)} artifact(s), "
          f"{len(release.dependencies)} dependency/ies, SBOM available={release.sbom.available}")
    print("Upgrade checklist:")
    for step in release.upgrade_checklist:
        print(f"  - {step.step}: {step.detail}")
    return 0


def cmd_backup(args: argparse.Namespace) -> int:
    scopes = [BackupScope(s.strip()) for s in args.scopes.split(",")] if args.scopes else list(BackupScope)
    record = backup_manager.create_backup(scopes, args.dest, backup_type=BackupType(args.type))
    print(f"Backup {record.id} ({record.backup_type.value}, status={record.status}) written to {record.location}")
    if record.notes:
        for note in record.notes:
            print(f"  note: {note}")
    return 0 if record.status != "failed" else 1


def cmd_restore(args: argparse.Namespace) -> int:
    if not os.path.isfile(args.backup_path):
        print(f"Backup file not found: {args.backup_path}")
        return 1
    with open(args.backup_path, "r", encoding="utf-8") as f:
        raw = json.load(f)
    backup = BackupRecord(id=raw["backup_id"], location=args.backup_path, checksum=raw["checksum"],
                           scopes=[BackupScope(s) for s in raw["scopes"]], status="completed")
    mode = RestoreMode(args.mode)
    plan = restore_manager.plan_restore(backup, mode=mode)
    print(f"Restore plan {plan.id} ({plan.mode.value}): status={plan.status}")
    for issue in plan.validation_issues:
        print(f"  issue: {issue}")

    if mode == RestoreMode.DRY_RUN:
        print("Dry run only — no data was modified.")
        return 0
    if not args.yes:
        print("Refusing to apply restore without --yes (restores never overwrite data without confirmation).")
        return 1
    try:
        applied = restore_manager.apply_restore(plan, backup, confirm=True)
    except Exception as e:  # noqa: BLE001
        print(f"Restore failed: {e}")
        return 1
    print(f"Restore {applied.status}.")
    return 0 if applied.status == "applied" else 1


def cmd_config(args: argparse.Namespace) -> int:
    profile = deployment_profiles.get_profile(args.environment)
    print(configuration_manager.export(profile.sections, args.format))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    profile = deployment_profiles.get_profile(args.environment)
    report = environment_validator.run_environment_checks(args.environment)
    print(f"Environment: {args.environment}")
    print(f"Sections configured: {sorted(profile.sections.keys())}")
    print(f"Overall environment status: {report.overall_status.value}")
    return 0


def cmd_checklist(args: argparse.Namespace) -> int:
    report = environment_validator.run_environment_checks(args.environment)
    print(f"[{'x' if report.ready else ' '}] Environment checks pass ({report.overall_status.value})")
    print("[ ] A backup has been taken (run `production backup` and re-check)")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="production_cli.py", description="M4.9 Production Readiness CLI")
    parser.add_argument("--db-url", default=None, help="Async SQLAlchemy URL for persistence")
    subparsers = parser.add_subparsers(dest="command", required=True)

    production_parser = subparsers.add_parser("production", help="Production readiness commands")
    sub = production_parser.add_subparsers(dest="subcommand", required=True)

    check_p = sub.add_parser("check")
    check_p.add_argument("--environment", default="production")
    check_p.set_defaults(func=cmd_check)

    validate_p = sub.add_parser("validate")
    validate_p.add_argument("assets_dir")
    validate_p.add_argument("--environment", default="production")
    validate_p.set_defaults(func=cmd_validate)

    release_p = sub.add_parser("release")
    release_p.add_argument("version")
    release_p.add_argument("--previous", default=None)
    release_p.add_argument("--notes", nargs="*", default=None)
    release_p.set_defaults(func=cmd_release)

    backup_p = sub.add_parser("backup")
    backup_p.add_argument("--scopes", default=None, help="comma-separated BackupScope values")
    backup_p.add_argument("--dest", default="./backups")
    backup_p.add_argument("--type", default="full", choices=("full", "incremental"))
    backup_p.set_defaults(func=cmd_backup)

    restore_p = sub.add_parser("restore")
    restore_p.add_argument("backup_path")
    restore_p.add_argument("--mode", default="dry_run", choices=("dry_run", "full", "selective"))
    restore_p.add_argument("--yes", action="store_true")
    restore_p.set_defaults(func=cmd_restore)

    config_p = sub.add_parser("config")
    config_p.add_argument("--environment", default="production")
    config_p.add_argument("--format", default="yaml", choices=("yaml", "json", "env"))
    config_p.set_defaults(func=cmd_config)

    status_p = sub.add_parser("status")
    status_p.add_argument("--environment", default="production")
    status_p.set_defaults(func=cmd_status)

    checklist_p = sub.add_parser("checklist")
    checklist_p.add_argument("--environment", default="production")
    checklist_p.set_defaults(func=cmd_checklist)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
