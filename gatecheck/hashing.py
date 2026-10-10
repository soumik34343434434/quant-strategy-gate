from __future__ import annotations

import hashlib
import json
from pathlib import Path

IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".venv", ".mypy_cache"}
IGNORED_FILES = {".DS_Store"}


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_json(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def tree_manifest(root: Path) -> dict[str, str]:
    """{relative posix path: sha256} for every regular file under root. Symlinks are refused."""
    root = Path(root)
    if not root.is_dir():
        raise ValueError(f"not a directory: {root}")
    out: dict[str, str] = {}
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if any(part in IGNORED_DIRS for part in rel.parts) or p.name in IGNORED_FILES or p.suffix == ".pyc":
            continue
        if p.is_symlink():
            raise ValueError(f"symlink not allowed: {rel.as_posix()}")
        if p.is_file():
            out[rel.as_posix()] = sha256_file(p)
    return out


def manifest_sha256(files: dict[str, str]) -> str:
    lines = "".join(f"{path}\0{digest}\n" for path, digest in sorted(files.items()))
    return sha256_bytes(lines.encode())


def tree_sha256(root: Path) -> str:
    return manifest_sha256(tree_manifest(root))
