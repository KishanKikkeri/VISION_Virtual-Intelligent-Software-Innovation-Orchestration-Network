#!/usr/bin/env python3
"""
scripts/ci/generate_docs.py
=================================
CI/CD Quality Gate — "Auto-generate Mermaid docs during builds."

    python scripts/ci/generate_docs.py                # regenerate docs/workflows/ in place
    python scripts/ci/generate_docs.py --check         # generate into a scratch dir and
                                                        # diff against the committed docs/workflows/;
                                                        # exit 1 if they differ (drift = stale docs
                                                        # committed alongside a graph change — the
                                                        # PR should have regenerated them)
    python scripts/ci/generate_docs.py --out docs/workflows

Wraps graph_exporter.write_mermaid_docs() (Mermaid-only pages + a bare
index) and workflow_docs.write_all_docs() (fuller narrative pages that
supersede those with the same filenames, plus the richer index) — the
same two-step sequence used to produce docs/workflows/ in M3.10.
"""
from __future__ import annotations

import argparse
import difflib
import filecmp
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def _generate(out_dir: str) -> list[str]:
    from services.integration.diagnostics import graph_exporter, workflow_docs
    written = graph_exporter.write_mermaid_docs(output_dir=out_dir)
    written = workflow_docs.write_all_docs(output_dir=out_dir)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default="docs/workflows", help="output directory (default: docs/workflows)")
    parser.add_argument("--check", action="store_true",
                         help="don't write to --out; compare freshly generated docs against it and "
                              "fail if they differ")
    args = parser.parse_args()

    if not args.check:
        written = _generate(args.out)
        print(f"Wrote {len(written)} file(s) to {args.out}/:")
        for p in sorted(written):
            print(f"  {p}")
        return 0

    committed_dir = Path(args.out)
    with tempfile.TemporaryDirectory() as tmp:
        _generate(tmp)
        if not committed_dir.exists():
            print(f"FAIL: {committed_dir} does not exist — run without --check first to generate it.")
            return 1

        committed_files = {p.name for p in committed_dir.glob("*.md")}
        fresh_files = {p.name for p in Path(tmp).glob("*.md")}

        drift_found = False
        if committed_files != fresh_files:
            print("FAIL: file set differs.")
            print(f"  only in committed docs: {sorted(committed_files - fresh_files)}")
            print(f"  only in freshly generated docs: {sorted(fresh_files - committed_files)}")
            drift_found = True

        for name in sorted(committed_files & fresh_files):
            committed_path, fresh_path = committed_dir / name, Path(tmp) / name
            if not filecmp.cmp(committed_path, fresh_path, shallow=False):
                drift_found = True
                print(f"FAIL: {name} is stale relative to the current graph definitions.")
                diff = difflib.unified_diff(
                    committed_path.read_text().splitlines(keepends=True),
                    fresh_path.read_text().splitlines(keepends=True),
                    fromfile=f"committed/{name}", tofile=f"freshly-generated/{name}",
                )
                sys.stdout.writelines(list(diff)[:40])
                print("  ... (diff truncated)" if len(list(diff)) > 40 else "")

        if drift_found:
            print()
            print("Docs are out of date. Run: python scripts/ci/generate_docs.py")
            print("and commit the result.")
            return 1

        print(f"OK: {args.out}/ matches freshly generated docs ({len(committed_files)} files checked).")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
