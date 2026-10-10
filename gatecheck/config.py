"""Versioned, hashed, append-only config.

config/
  registry.json        hash-chained log of every version
  v1/gates.json        thresholds and conventions
  v1/allowed_signers   risk-officer public keys
  v2/...

The last registry entry is the active version. Nothing is trusted until the
whole chain and every listed file hash verify.
"""
from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigError
from .hashing import canonical_json, sha256_bytes, sha256_file

REGISTRY = "registry.json"
FILES = ("gates.json", "allowed_signers")
GENESIS = "0" * 64
ENTRY_KEYS = {"version", "date", "files", "changes", "reason", "prev_entry_sha256", "entry_sha256"}
REQUIRED_SECTIONS = {
    "benchmarks": None,
    "integrity": ("gross_pnl_rel_tol", "equity_reconcile_rel_tol", "summary_sharpe_abs_tol", "summary_rel_tol"),
    "deployment_limits": ("max_live_strategies", "max_assets", "max_initial_allocation_fraction",
                          "kill_switch_drawdown"),
}
REQUIRED_GATES = {
    "settings": ("min_slippage_bps", "realized_rate_min_fraction"),
    "sample": ("min_closed_trades",),
    "quality": ("min_sharpe_exclusive", "min_profit_factor_exclusive"),
    "pain": ("max_drawdown_exclusive",),
    "neutral_book": ("max_abs_beta", "min_obs_total", "min_obs_each_side"),
    "risk_officer": ("signature_namespace",),
    "stress": ("fee_multiplier", "slippage_multiplier"),
    "plateau": ("min_neighbor_ratio",),
    "recent": ("trailing_months",),
    "forward": ("min_days", "max_gap_days", "max_abs_return_diff", "max_trade_count_diff_rel", "min_shadow_trades"),
    "selection": ("min_deflated_sharpe",),
}


@dataclass(frozen=True)
class ActiveConfig:
    version: str
    data: dict
    dir: Path
    files: dict
    registry_head: str

    @property
    def allowed_signers(self) -> Path:
        return self.dir / "allowed_signers"

    def gate(self, key: str) -> dict:
        return self.data["gates"][key]


def _reject_constant(name):
    raise ValueError(f"non-finite number {name} not allowed")


def _read_json(path: Path):
    return json.loads(Path(path).read_text(encoding="utf-8"), parse_constant=_reject_constant)


def entry_hash(entry: dict) -> str:
    return sha256_bytes(canonical_json({k: v for k, v in entry.items() if k != "entry_sha256"}))


def _verify_chain(config_dir: Path) -> list[dict]:
    reg = _read_json(config_dir / REGISTRY)
    entries = reg.get("entries") if isinstance(reg, dict) else None
    if not isinstance(entries, list) or not entries:
        raise ValueError("registry has no entries")
    prev = GENESIS
    for i, e in enumerate(entries):
        if not isinstance(e, dict) or set(e) != ENTRY_KEYS:
            raise ValueError(f"registry entry {i + 1} has the wrong fields")
        if e["version"] != f"v{i + 1}":
            raise ValueError(f"registry entry {i + 1} has version {e['version']!r}")
        if e["prev_entry_sha256"] != prev:
            raise ValueError(f"registry chain broken at {e['version']}")
        if entry_hash(e) != e["entry_sha256"]:
            raise ValueError(f"registry entry {e['version']} was edited after it was written")
        if not isinstance(e["files"], dict) or set(e["files"]) != set(FILES):
            raise ValueError(f"registry entry {e['version']} must list exactly {FILES}")
        for name, digest in e["files"].items():
            path = config_dir / e["version"] / name
            if not path.is_file():
                raise ValueError(f"{e['version']}/{name} is missing")
            if sha256_file(path) != digest:
                raise ValueError(f"{e['version']}/{name} hash does not match the registry")
        prev = e["entry_sha256"]
    return entries


def _validate_shape(data: dict, version: str) -> None:
    if not isinstance(data, dict) or data.get("version") != version:
        raise ValueError(f"gates.json does not declare version {version}")
    for section, keys in REQUIRED_SECTIONS.items():
        if not isinstance(data.get(section), dict):
            raise ValueError(f"config section {section!r} missing")
        for k in keys or ():
            if k not in data[section]:
                raise ValueError(f"config value {section}.{k} missing")
    gates = data.get("gates")
    if not isinstance(gates, dict):
        raise ValueError("config section 'gates' missing")
    for g, keys in REQUIRED_GATES.items():
        if not isinstance(gates.get(g), dict):
            raise ValueError(f"config for gate {g!r} missing")
        for k in keys:
            if k not in gates[g]:
                raise ValueError(f"config value gates.{g}.{k} missing")
    for sym, b in data["benchmarks"].items():
        if not isinstance(b, dict) or not _is_pos_int(b.get("periods_per_year")) or not _is_pos_num(b.get("max_gap_days")):
            raise ValueError(f"benchmark {sym!r} needs integer periods_per_year and positive max_gap_days")


def _is_pos_num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0


def _is_pos_int(x) -> bool:
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def load_active(config_dir: Path) -> ActiveConfig:
    config_dir = Path(config_dir)
    try:
        entries = _verify_chain(config_dir)
        head = entries[-1]
        vdir = config_dir / head["version"]
        data = _read_json(vdir / "gates.json")
        _validate_shape(data, head["version"])
    except ConfigError:
        raise
    except Exception as e:  # any failure to verify is a failure, full stop
        raise ConfigError(f"config verification failed: {e}") from e
    return ActiveConfig(version=head["version"], data=data, dir=vdir, files=dict(head["files"]),
                        registry_head=head["entry_sha256"])


def _write_atomic(path: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def _write_registry(config_dir: Path, entries: list[dict]) -> None:
    _write_atomic(config_dir / REGISTRY,
                  json.dumps({"schema": "strategy-gate-registry/1", "entries": entries}, indent=2) + "\n")


def _new_entry(version, date, vdir, changes, reason, prev) -> dict:
    entry = {
        "version": version,
        "date": date,
        "files": {name: sha256_file(vdir / name) for name in FILES},
        "changes": changes,
        "reason": reason,
        "prev_entry_sha256": prev,
    }
    entry["entry_sha256"] = entry_hash(entry)
    return entry


def init_registry(config_dir: Path, date: str, reason: str) -> None:
    config_dir = Path(config_dir)
    if (config_dir / REGISTRY).exists():
        raise ValueError("registry already exists; use create_version")
    if not reason.strip():
        raise ValueError("a reason is required")
    data = _read_json(config_dir / "v1" / "gates.json")
    _validate_shape(data, "v1")
    _write_registry(config_dir, [_new_entry("v1", date, config_dir / "v1", [], reason, GENESIS)])


def _same_kind(old, new) -> bool:
    if isinstance(old, bool) or isinstance(new, bool):
        return isinstance(old, bool) and isinstance(new, bool)
    if isinstance(old, (int, float)):
        return isinstance(new, (int, float))
    return type(old) is type(new)


def create_version(config_dir: Path, sets: dict, reason: str, date: str,
                   allowed_signers: Path | None = None) -> str:
    """The only way to change a threshold, benchmark or reviewer key. Returns the new version."""
    config_dir = Path(config_dir)
    active = load_active(config_dir)
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("a reason is required")
    if not sets and allowed_signers is None:
        raise ValueError("no changes requested")
    data = copy.deepcopy(active.data)
    changes = []
    for key, value in sets.items():
        parts = key.split(".")
        if parts[0] in ("version", "schema") or not all(parts):
            raise ValueError(f"{key!r} cannot be set")
        parent = data
        for p in parts[:-1]:
            if not isinstance(parent, dict) or p not in parent:
                raise ValueError(f"unknown config key {key!r}")
            parent = parent[p]
        leaf = parts[-1]
        if not isinstance(parent, dict):
            raise ValueError(f"unknown config key {key!r}")
        if leaf in parent:
            old = parent[leaf]
            if not _same_kind(old, value):
                raise ValueError(f"{key!r}: {value!r} is not the same type as {old!r}")
        elif len(parts) == 2 and parts[0] == "benchmarks":
            if not (isinstance(value, dict) and _is_pos_int(value.get("periods_per_year"))
                    and _is_pos_num(value.get("max_gap_days"))):
                raise ValueError(f"{key!r} needs periods_per_year (int) and max_gap_days")
            old = None
        else:
            raise ValueError(f"unknown config key {key!r}")
        if old == value:
            raise ValueError(f"{key!r} is already {value!r}")
        parent[leaf] = value
        changes.append({"key": key, "old": old, "new": value})

    reg = _read_json(config_dir / REGISTRY)
    entries = reg["entries"]
    version = f"v{len(entries) + 1}"
    data["version"] = version
    _validate_shape(data, version)
    vdir = config_dir / version
    vdir.mkdir()
    (vdir / "gates.json").write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    signers_src = Path(allowed_signers) if allowed_signers is not None else active.allowed_signers
    (vdir / "allowed_signers").write_bytes(signers_src.read_bytes())
    if allowed_signers is not None:
        changes.append({"key": "allowed_signers", "old": active.files["allowed_signers"],
                        "new": sha256_file(vdir / "allowed_signers")})
    entries.append(_new_entry(version, date, vdir, changes, reason.strip(), entries[-1]["entry_sha256"]))
    _write_registry(config_dir, entries)
    load_active(config_dir)  # the new version must verify before we report success
    return version


def is_append_only(old: dict, new: dict) -> bool:
    a, b = old.get("entries", []), new.get("entries", [])
    return len(b) >= len(a) and b[: len(a)] == a
