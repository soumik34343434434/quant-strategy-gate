#!/usr/bin/env python3
"""python strategy_hash.py <strategy_dir>  ->  prints the strategy_version.

sha256 over every file in the strategy source directory (paths and contents,
params file included). Any edit to any file is a new version.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

if __name__ == "__main__":
    if not sys.flags.isolated:
        os.execv(sys.executable, [sys.executable, "-I", str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(HERE))
    from gatecheck.hashing import tree_sha256

    if len(sys.argv) != 2 or sys.argv[1].startswith("-"):
        print("usage: python strategy_hash.py <strategy_dir>", file=sys.stderr)
        sys.exit(2)
    try:
        print(tree_sha256(Path(sys.argv[1])))
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
