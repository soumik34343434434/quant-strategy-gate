"""CI: the config chain verifies, and the registry only grew relative to the base commit.

  python -I ci/check_registry.py <base_sha>
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from gatecheck import config as C  # noqa: E402


def base_registry(base: str):
    if not base or set(base) == {"0"}:
        return None
    r = subprocess.run(["git", "show", f"{base}:config/registry.json"], cwd=ROOT, capture_output=True, text=True)
    return json.loads(r.stdout) if r.returncode == 0 else None


def main(argv) -> int:
    try:
        active = C.load_active(ROOT / "config")
    except Exception as e:
        print(f"FAIL: {e}")
        return 1
    print(f"config chain verifies; active version {active.version}")
    old = base_registry(argv[0] if argv else "")
    if old is None:
        print("no base registry to compare against (first commit or new branch)")
        return 0
    new = json.loads((ROOT / "config" / "registry.json").read_text())
    if not C.is_append_only(old, new):
        print("FAIL: config/registry.json rewrote history. Threshold changes must be new versions.")
        return 1
    added = len(new["entries"]) - len(old["entries"])
    for e in new["entries"][len(old["entries"]):]:
        print(f"new config {e['version']} ({e['date']}): {e['reason']}")
        for c in e["changes"]:
            print(f"  {c['key']}: {c['old']!r} -> {c['new']!r}")
    print(f"append-only: ok ({added} new version(s))")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
