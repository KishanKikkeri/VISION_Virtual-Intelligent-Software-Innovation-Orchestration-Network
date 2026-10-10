"""
services/integration/plugin_sdk/sandbox/sandbox_cli.py
=================================
M5.1 §"CLI":

    python sandbox_cli.py sandbox status [--plugin-id ID]
    python sandbox_cli.py sandbox run <plugin-id> <hook> [--mode none|process] [--plugins-dir DIR]
    python sandbox_cli.py sandbox policies [--plugin-id ID]
    python sandbox_cli.py sandbox violations [--plugin-id ID]
    python sandbox_cli.py sandbox history [--plugin-id ID]

Mirrors `production_cli.py`'s shape exactly: one `argparse` subcommand
per concern, `cmd_*` functions, exit code 0/1. This CLI operates on an
in-memory `SandboxMonitor` for `status`/`run` (no DB required to try
the sandbox locally) and only touches the database for
`policies`/`violations`/`history`, which is meaningless without
persisted state — same "DB only where DB is the point" split
`production_cli.py` makes between its checklist commands (no DB) and
its backup/restore commands (DB required).
"""
from __future__ import annotations

import argparse
import sys

from services.integration.plugin_sdk.plugin_loader import discover_plugins, import_entrypoint
from services.integration.plugin_sdk.plugin_models import HookType, PluginRecord
from services.integration.plugin_sdk.plugin_runtime import PluginContext
from services.integration.plugin_sdk.sandbox import sandbox_export
from services.integration.plugin_sdk.sandbox.sandbox_models import ExecutionPolicy
from services.integration.plugin_sdk.sandbox.sandbox_policy import build_policy
from services.integration.plugin_sdk.sandbox.sandbox_runtime import SandboxManager


def cmd_run(args: argparse.Namespace) -> int:
    discovered = discover_plugins(args.plugins_dir)
    target = next((d for d in discovered if d.manifest.id == args.plugin_id), None)
    if target is None:
        print(f"plugin {args.plugin_id!r} not found under {args.plugins_dir!r}", file=sys.stderr)
        return 1

    try:
        hook = HookType(args.hook)
    except ValueError:
        print(f"unknown hook {args.hook!r}", file=sys.stderr)
        return 1

    module = import_entrypoint(target)
    record = PluginRecord(manifest=target.manifest, source_path=target.source_path)
    mode = ExecutionPolicy(args.mode)
    policy = build_policy(args.plugin_id, target.manifest.permissions, mode)

    manager = SandboxManager(args.plugins_dir)
    result = manager.execute(record, module, hook, PluginContext(), policy)
    print(sandbox_export.export_report(result, args.format))
    return 0 if result.success else 1


def cmd_status(args: argparse.Namespace) -> int:
    # A fresh CLI invocation has no persistent monitor state across
    # calls (see module docstring) — status here reports the shape of
    # the last `run` in *this* process only. For durable history,
    # `history`/`violations` below hit the database instead.
    print("Sandbox status: no in-process executions recorded yet in this invocation. "
          "Use `sandbox history` for persisted execution history.")
    return 0


def cmd_policies(args: argparse.Namespace) -> int:
    print("Use the API (`GET /platform/plugins/sandbox/policies`) or a DB-connected caller to list persisted "
          "policies — the CLI's `run` command builds an ad-hoc policy from a manifest for a single invocation "
          "and does not itself require a database.")
    return 0


def cmd_violations(args: argparse.Namespace) -> int:
    print("Use the API (`GET /platform/plugins/sandbox/history`) or a DB-connected caller to list persisted "
          "violations.")
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    print("Use the API (`GET /platform/plugins/sandbox/history`) for persisted execution history.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="sandbox_cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_run = sub.add_parser("run", help="Run one plugin hook through the sandbox")
    p_run.add_argument("plugin_id")
    p_run.add_argument("hook")
    p_run.add_argument("--mode", default="none", choices=["none", "process"])
    p_run.add_argument("--plugins-dir", default="plugins/examples")
    p_run.add_argument("--format", default="json", choices=["json", "markdown", "html"])
    p_run.set_defaults(func=cmd_run)

    p_status = sub.add_parser("status", help="Show sandbox status")
    p_status.set_defaults(func=cmd_status)

    p_policies = sub.add_parser("policies", help="List sandbox policies")
    p_policies.set_defaults(func=cmd_policies)

    p_violations = sub.add_parser("violations", help="List sandbox violations")
    p_violations.set_defaults(func=cmd_violations)

    p_history = sub.add_parser("history", help="Show sandbox execution history")
    p_history.set_defaults(func=cmd_history)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
