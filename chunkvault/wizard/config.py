"""User-level config: registered repos, source paths.

Persists to ``~/.chunkvault/config.json`` (the same file the i18n
locale picker uses — each module preserves the other's keys when
writing). Two registries live here:

- ``repos``: vaults the user has told chunkvault to know about. Listed
  by the wizard on launch even when run from a directory that doesn't
  contain a vault.
- ``source_paths``: directories the user wants the ingest flow to
  always scan for backup archives.

Both are resolved-and-deduped on add. Removal is by exact resolved
path. Order is preserved (list, not set), so ``repo list`` shows them
in the order the user added them.

Reads tolerate every kind of malformed input — missing file, bad JSON,
non-list values for the registry keys — by returning ``[]``. Writes
always preserve unrelated keys (locale, future additions).
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

_CONFIG_DIR = Path.home() / ".chunkvault"
_CONFIG_PATH = _CONFIG_DIR / "config.json"


@dataclass(frozen=True)
class RegisteredRepo:
    path: Path
    label: str = ""


@dataclass(frozen=True)
class RegisteredSource:
    path: Path
    label: str = ""


# ---- raw JSON I/O ----------------------------------------------------------

def _load_raw() -> dict:
    try:
        if not _CONFIG_PATH.is_file():
            return {}
        data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_raw(data: dict) -> Path | None:
    try:
        _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        _CONFIG_PATH.write_text(
            json.dumps(data, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        return _CONFIG_PATH
    except OSError:
        return None


def config_path() -> Path:
    return _CONFIG_PATH


# ---- registered repos ------------------------------------------------------

def list_repos() -> list[RegisteredRepo]:
    raw = _load_raw().get("repos", [])
    if not isinstance(raw, list):
        return []
    out: list[RegisteredRepo] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not isinstance(p, str) or not p:
            continue
        label = entry.get("label", "") or ""
        out.append(RegisteredRepo(path=Path(p), label=str(label)))
    return out


def add_repo(path: Path | str, *, label: str = "") -> bool:
    """Register a vault path. Returns True if newly added, False if it was
    already registered. Path is stored resolved+absolute for stable lookup."""
    p = Path(path).expanduser().resolve()
    data = _load_raw()
    repos = data.setdefault("repos", [])
    if not isinstance(repos, list):
        repos = []
        data["repos"] = repos
    p_str = str(p)
    for entry in repos:
        if isinstance(entry, dict) and entry.get("path") == p_str:
            # Update label if changed
            if label and entry.get("label") != label:
                entry["label"] = label
                _save_raw(data)
            return False
    repos.append({"path": p_str, "label": label})
    _save_raw(data)
    return True


def remove_repo(path: Path | str) -> bool:
    """Drop a registered vault path. Returns True if removed, False if it
    wasn't registered."""
    p = Path(path).expanduser().resolve()
    data = _load_raw()
    repos = data.get("repos", [])
    if not isinstance(repos, list):
        return False
    p_str = str(p)
    new_repos = [
        e for e in repos
        if not (isinstance(e, dict) and e.get("path") == p_str)
    ]
    if len(new_repos) == len(repos):
        return False
    data["repos"] = new_repos
    _save_raw(data)
    return True


# ---- registered source paths -----------------------------------------------

def list_source_paths() -> list[RegisteredSource]:
    raw = _load_raw().get("source_paths", [])
    if not isinstance(raw, list):
        return []
    out: list[RegisteredSource] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        p = entry.get("path")
        if not isinstance(p, str) or not p:
            continue
        label = entry.get("label", "") or ""
        out.append(RegisteredSource(path=Path(p), label=str(label)))
    return out


def add_source_path(path: Path | str, *, label: str = "") -> bool:
    p = Path(path).expanduser().resolve()
    data = _load_raw()
    sources = data.setdefault("source_paths", [])
    if not isinstance(sources, list):
        sources = []
        data["source_paths"] = sources
    p_str = str(p)
    for entry in sources:
        if isinstance(entry, dict) and entry.get("path") == p_str:
            if label and entry.get("label") != label:
                entry["label"] = label
                _save_raw(data)
            return False
    sources.append({"path": p_str, "label": label})
    _save_raw(data)
    return True


def remove_source_path(path: Path | str) -> bool:
    p = Path(path).expanduser().resolve()
    data = _load_raw()
    sources = data.get("source_paths", [])
    if not isinstance(sources, list):
        return False
    p_str = str(p)
    new_sources = [
        e for e in sources
        if not (isinstance(e, dict) and e.get("path") == p_str)
    ]
    if len(new_sources) == len(sources):
        return False
    data["source_paths"] = new_sources
    _save_raw(data)
    return True
