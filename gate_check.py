#!/usr/bin/env python3
"""python gate_check.py <report_dir>

Exit 0 = DEPLOYABLE, exit 1 = BLOCKED, exit 2 = usage error.

There are no options. Thresholds come only from the hash-verified config next
to this file. The process re-executes itself under `python -I`, so PYTHONPATH
and other interpreter environment variables cannot inject code. Any unexpected
error is BLOCKED.
"""
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
USAGE = "usage: python gate_check.py <report_dir>    (there are no options)"


def _isolate() -> None:
    if not sys.flags.isolated:
        os.execv(sys.executable, [sys.executable, "-I", str(Path(__file__).resolve()), *sys.argv[1:]])
    sys.path.insert(0, str(HERE))


def main(argv: list[str]) -> int:
    if len(argv) != 1 or argv[0].startswith("-"):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        from gatecheck.runner import run_check
        from gatecheck.verdict import write_outputs

        verdict = run_check(Path(argv[0]), HERE / "config")
        out = write_outputs(verdict, HERE / "verdicts")
        print(verdict.summary_text())
        print(f"verdict files: {out}")
        return verdict.exit_code
    except Exception as e:
        print(f"BLOCKED: checker error: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    _isolate()
    sys.exit(main(sys.argv[1:]))
