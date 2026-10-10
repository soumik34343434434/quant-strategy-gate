"""CI: run the gate on every report bundle under reports/ touched since the base commit.

  python -I ci/check_changed_reports.py <base_sha>

A bundle is any directory under reports/ containing manifest.json. If the base
is unknown, every bundle is checked. Any BLOCKED fails the job. A config change
re-checks every bundle, because verdicts are bound to the config version.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"


def all_bundles() -> set[Path]:
    return {p.parent for p in REPORTS.rglob("manifest.json")} if REPORTS.is_dir() else set()


def changed_bundles(base: str) -> set[Path]:
    if not base or set(base) == {"0"}:
        return all_bundles()
    r = subprocess.run(["git", "diff", "--name-only", f"{base}...HEAD"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return all_bundles()
    paths = [ROOT / line for line in r.stdout.splitlines() if line.strip()]
    if any(p.is_relative_to(ROOT / "config") or p.is_relative_to(ROOT / "gatecheck") for p in paths):
        return all_bundles()
    out = set()
    for p in paths:
        for parent in [p, *p.parents]:
            if parent == REPORTS or not parent.is_relative_to(REPORTS):
                break
            if (parent / "manifest.json").is_file():
                out.add(parent)
                break
    return out


def main(argv) -> int:
    bundles = sorted(changed_bundles(argv[0] if argv else ""))
    if not bundles:
        print("no strategy report bundles changed")
        return 0
    worst = 0
    for b in bundles:
        r = subprocess.run([sys.executable, "-I", str(ROOT / "gate_check.py"), str(b)], cwd=ROOT)
        print(f"{'DEPLOYABLE' if r.returncode == 0 else 'BLOCKED'}: {b.relative_to(ROOT)}")
        worst = max(worst, 0 if r.returncode == 0 else 1)
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
