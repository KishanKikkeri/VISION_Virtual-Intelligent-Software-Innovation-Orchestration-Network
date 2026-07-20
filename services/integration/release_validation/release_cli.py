"""
services/integration/release_validation/release_cli.py
=================================
M4.10 §1 CLI:

    python release_cli.py release check <version> [--environment production] [--format markdown]
    python release_cli.py release docs [--out docs/generated]
    python release_cli.py release install-check
    python release_cli.py release benchmark [--results benchmarks/benchmark.json]
    python release_cli.py release qa
    python release_cli.py release package <version>

Mirrors M4.9's `production_cli.py` shape: one `argparse` subcommand per
concern, functions named `cmd_*`, exit code 0/1 on ready/not-ready.
"""
from __future__ import annotations

import argparse
import json
import sys

from services.integration.release_validation import release_export
from services.integration.release_validation.readiness_report import build_readiness_report
from services.integration.release_validation.release_validation_models import ExportFormat


def cmd_check(args: argparse.Namespace) -> int:
    report = build_readiness_report(args.version, environment=args.environment)
    print(release_export.export_report(report, args.format))
    return 0 if report.release_candidate_ready else 1


def cmd_docs(args: argparse.Namespace) -> int:
    from services.integration.release_validation import documentation_generator
    written = documentation_generator.generate_all(out_dir=args.out)
    for path in written:
        print(f"wrote {path}")
    return 0


def cmd_install_check(args: argparse.Namespace) -> int:
    from scripts.install import verify as install_verify
    report = install_verify.run_all_checks()
    for c in report.checks:
        print(f"  [{c.status.value}] {c.name}: {c.detail}")
    return 0 if report.ready else 1


def cmd_benchmark(args: argparse.Namespace) -> int:
    from benchmarks import runner as benchmark_runner
    summary = benchmark_runner.run_benchmarks()
    print(json.dumps(summary.model_dump(mode="json"), indent=2))
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    from services.integration.release_validation import final_qa
    report = final_qa.run_final_qa()
    for c in report.checks:
        print(f"  [{c.status.value}] {c.name}: {c.detail}")
    return 0 if report.passed else 1


def cmd_package(args: argparse.Namespace) -> int:
    from services.integration.release_validation import release_packaging
    manifest = release_packaging.build_release_manifest(args.version)
    print(json.dumps(manifest.model_dump(mode="json"), indent=2))
    return 0 if manifest.complete else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="release_cli.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_check = sub.add_parser("check", help="Run the unified release readiness report")
    p_check.add_argument("version")
    p_check.add_argument("--environment", default="production")
    p_check.add_argument("--format", default="markdown", choices=[f.value for f in ExportFormat])
    p_check.set_defaults(func=cmd_check)

    p_docs = sub.add_parser("docs", help="Generate docs/generated/*")
    p_docs.add_argument("--out", default="docs/generated")
    p_docs.set_defaults(func=cmd_docs)

    p_install = sub.add_parser("install-check", help="Run the installation wizard's verification checks")
    p_install.set_defaults(func=cmd_install_check)

    p_bench = sub.add_parser("benchmark", help="Run the benchmark suite")
    p_bench.set_defaults(func=cmd_benchmark)

    p_qa = sub.add_parser("qa", help="Run Final QA")
    p_qa.set_defaults(func=cmd_qa)

    p_pkg = sub.add_parser("package", help="Build the release manifest")
    p_pkg.add_argument("version")
    p_pkg.set_defaults(func=cmd_package)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
