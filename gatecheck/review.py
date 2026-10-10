"""Gate 7: the risk officer's review (SPEC §5).

The review is untrusted text. It must name the exact strategy version, a
reviewer other than the builder, end with a single unambiguous verdict line,
and carry an SSH signature that verifies against the reviewer keys pinned
in the config. The builder never signs off their own work.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

# Fixed locations only: resolving through PATH would let the environment pick the verifier.
SSH_KEYGEN_CANDIDATES = ("/usr/bin/ssh-keygen", "/usr/local/bin/ssh-keygen", "/opt/homebrew/bin/ssh-keygen")
VERDICT_RE = re.compile(r"^VERDICT:\s*(KEEP|KILL)\s*[—–-]\s*(\S.*)$")
REVIEWER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")
SIG_HEADER = b"-----BEGIN SSH SIGNATURE-----"


def _ssh_keygen() -> str | None:
    for c in SSH_KEYGEN_CANDIDATES:
        if Path(c).is_file():
            return c
    return None


def _field(lines: list[str], key: str) -> tuple[str | None, str | None]:
    hits = [ln.split(":", 1)[1].strip() for ln in lines if ln.strip().startswith(f"{key}:")]
    if not hits:
        return None, f"no '{key}:' line"
    if len(hits) > 1:
        return None, f"more than one '{key}:' line"
    return hits[0], None


def verify_signature(doc: Path, sig: Path, allowed_signers: Path, principal: str, namespace: str) -> str | None:
    """Returns None if the signature verifies, else the reason it does not."""
    exe = _ssh_keygen()
    if exe is None:
        return "ssh-keygen not found; signature cannot be verified"
    if not sig.is_file():
        return f"signature file {sig.name} is missing (reviews must be signed)"
    if not sig.read_bytes().lstrip().startswith(SIG_HEADER):
        return f"signature file {sig.name} is not an SSH signature"
    try:
        r = subprocess.run([exe, "-Y", "verify", "-f", str(allowed_signers), "-I", principal, "-n", namespace,
                            "-s", str(sig)], input=doc.read_bytes(), capture_output=True, timeout=30,
                           env={"LC_ALL": "C"})
    except Exception as e:
        return f"signature verification could not run: {e}"
    if r.returncode != 0:
        return (f"signature does not verify for reviewer {principal!r} against the config's allowed_signers "
                f"(namespace {namespace!r})")
    return None


def evaluate(root: Path, manifest: dict, allowed_signers: Path, namespace: str) -> tuple[str | None, list[str]]:
    """Returns (verdict, problems). The gate passes only if verdict == 'KEEP' and problems is empty."""
    doc = root / "risk_review.md"
    if not doc.is_file():
        return None, ["risk_review.md is missing"]
    try:
        text = doc.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return None, ["risk_review.md is not UTF-8 text"]
    lines = text.splitlines()
    problems: list[str] = []

    version, err = _field(lines, "strategy_version")
    if err:
        problems.append(f"risk_review.md has {err}")
    elif version != manifest["strategy_version"]:
        problems.append(f"risk_review.md reviews strategy_version {version[:12]}..., report is "
                        f"{manifest['strategy_version'][:12]}...")

    reviewer, err = _field(lines, "reviewer")
    if err:
        problems.append(f"risk_review.md has {err}")
    elif not REVIEWER_RE.match(reviewer):
        problems.append("reviewer id has invalid characters")
        reviewer = None
    elif reviewer.lower() == str(manifest["builder_id"]).strip().lower():
        problems.append("reviewer is the builder; the builder never signs off their own work")

    verdict = None
    verdict_lines = [ln for ln in lines if ln.strip().upper().startswith("VERDICT")]
    non_empty = [ln for ln in lines if ln.strip()]
    if not verdict_lines:
        problems.append("no VERDICT line")
    elif len(verdict_lines) > 1:
        problems.append("more than one VERDICT line")
    elif non_empty[-1] != verdict_lines[0]:
        problems.append("the VERDICT line must be the last line")
    else:
        m = VERDICT_RE.match(verdict_lines[0].strip())
        if not m:
            problems.append("VERDICT line must read 'VERDICT: KEEP — <reason>' or 'VERDICT: KILL — <reason>'")
        else:
            verdict = m.group(1)
            if verdict == "KILL":
                problems.append(f"risk officer verdict is KILL: {m.group(2).strip()[:200]}")

    if reviewer:
        err = verify_signature(doc, root / "risk_review.md.sig", allowed_signers, reviewer, namespace)
        if err:
            problems.append(err)
    else:
        problems.append("signature not checked: no valid reviewer id")
    return verdict, problems
