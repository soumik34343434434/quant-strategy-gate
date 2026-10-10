"""Gate 7: an independent, signed KEEP from someone other than the builder."""
import subprocess

import pytest

from tests.fixtures.builder import review_text
from tests.fixtures.helpers import detail, failed, reason, status

GOOD = "VERDICT: KEEP — drawdown profile acceptable"


def write_review(bundle, text, signer=None):
    p = bundle / "risk_review.md"
    p.write_text(text)
    if signer is not None:
        signer.sign(p)
    else:
        p.with_name("risk_review.md.sig").unlink(missing_ok=True)


def body(clean, reviewer="risk-officer-1", verdict_line=GOOD, version=None):
    return review_text(version or clean.spec.strategy_version, reviewer, verdict_line)


@pytest.mark.parametrize("line", [
    "VERDICT: KEEP — fine",
    "VERDICT: KEEP – fine",
    "VERDICT: KEEP - fine",
])
def test_accepted_separators(line, clean, check, keys):
    write_review(clean.path, body(clean, verdict_line=line), keys["officer"])
    v = check(clean.path)
    assert status(v, 7) == "PASS", detail(v)


BAD_TEXT = [
    ("no verdict line", lambda c: body(c, verdict_line="Looks reasonable to me."), "verdict"),
    ("lowercase", lambda c: body(c, verdict_line="verdict: keep — ok"), "verdict"),
    ("no reason", lambda c: body(c, verdict_line="VERDICT: KEEP"), "reason"),
    ("empty reason", lambda c: body(c, verdict_line="VERDICT: KEEP — "), "reason"),
    ("keep and kill", lambda c: body(c, verdict_line="VERDICT: KEEP and KILL — unsure"), "verdict"),
    ("two verdicts", lambda c: body(c, verdict_line="VERDICT: KEEP — ok\nVERDICT: KILL — no"), "verdict"),
    ("verdict not last", lambda c: body(c) + "\nPS: will revisit next week.\n", "last"),
    ("maybe", lambda c: body(c, verdict_line="VERDICT: MAYBE — needs more data"), "verdict"),
    ("reviewer is builder", lambda c: body(c, reviewer="builder-alice"), "builder"),
    ("reviewer is builder, case", lambda c: body(c, reviewer="Builder-Alice"), "builder"),
    ("wrong version", lambda c: body(c, version="c" * 64), "strategy_version"),
    ("no version", lambda c: body(c).replace("strategy_version:", "version:"), "strategy_version"),
    ("two versions", lambda c: body(c).replace("reviewer:", f"strategy_version: {'d' * 64}\nreviewer:"), "strategy_version"),
    ("no reviewer", lambda c: body(c).replace("reviewer:", "by:"), "reviewer"),
]


@pytest.mark.parametrize("label,text,needle", BAD_TEXT, ids=[b[0] for b in BAD_TEXT])
def test_bad_review_blocks_only_gate_7(label, text, needle, clean, check, keys):
    write_review(clean.path, text(clean), keys["officer"])
    v = check(clean.path)
    assert v.verdict == "BLOCKED"
    assert failed(v) == {7}, detail(v)
    assert needle.lower() in reason(v, 7).lower(), reason(v, 7)


def test_unsigned_review_blocks(clean, check):
    write_review(clean.path, body(clean), signer=None)
    v = check(clean.path)
    assert failed(v) == {7}, detail(v)
    assert "signature" in reason(v, 7).lower()


def test_review_signed_by_unregistered_key_blocks(clean, check, keys):
    write_review(clean.path, body(clean), keys["rogue"])
    v = check(clean.path)
    assert failed(v) == {7}, detail(v)
    assert "signature" in reason(v, 7).lower()


def test_review_edited_after_signing_blocks(clean, check, keys):
    write_review(clean.path, body(clean, verdict_line="VERDICT: KILL — leverage too high"), keys["officer"])
    p = clean.path / "risk_review.md"
    p.write_text(p.read_text().replace("VERDICT: KILL — leverage too high", GOOD))
    v = check(clean.path)
    assert failed(v) == {7}, detail(v)
    assert "signature" in reason(v, 7).lower()


def test_review_signed_in_wrong_namespace_blocks(clean, check, keys):
    p = clean.path / "risk_review.md"
    p.write_text(body(clean))
    sig = p.with_name("risk_review.md.sig")
    sig.unlink()
    subprocess.run(["ssh-keygen", "-q", "-Y", "sign", "-f", str(keys["officer"].key), "-n", "file", str(p)],
                   check=True, capture_output=True)
    v = check(clean.path)
    assert failed(v) == {7}, detail(v)


def test_review_impersonating_registered_reviewer_blocks(clean, check, keys):
    """A rogue key cannot claim to be the registered risk officer."""
    write_review(clean.path, body(clean, reviewer="risk-officer-1"), keys["rogue"])
    v = check(clean.path)
    assert failed(v) == {7}, detail(v)
