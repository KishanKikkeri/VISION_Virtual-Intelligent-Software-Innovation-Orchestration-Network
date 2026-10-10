"""
examples/documentation_generator/main.py
=================================
M4.10 §5 — demonstrates §2's Documentation Generator directly: runs
`documentation_generator.generate_all()` against the real repository
root and reports what it wrote. This is the one example that IS a
thin wrapper over a real M4.10 module (deliberately — showing the
generator's own output is the most concrete demonstration of "No
manual duplication" from §2).

Run: python examples/documentation_generator/main.py [--out docs/generated]
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..")))

from services.integration.release_validation.documentation_generator import generate_all


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="docs/generated")
    args = parser.parse_args()

    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    written = generate_all(root=root, out_dir=args.out)
    print(f"Generated {len(written)} document(s):")
    for path in written:
        size = os.path.getsize(path)
        print(f"  {path} ({size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
