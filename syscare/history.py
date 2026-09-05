"""Persistent maintenance history for GUI, CLI and scheduled runs."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HistoryEntry:
    timestamp: str
    action: str
    status: str
    summary: str
    reclaimed: int = 0
    details: dict[str, Any] | None = None


def state_dir() -> Path:
    override = os.environ.get("SYSCARE_STATE_HOME")
    if override:
        return Path(override)
    root = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    return root / "syscare"


def history_path() -> Path:
    return state_dir() / "history.json"


def list_entries(limit: int = 250) -> list[HistoryEntry]:
    if limit <= 0:
        return []
    try:
        raw = json.loads(history_path().read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return []
    entries: list[HistoryEntry] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        try:
            entries.append(
                HistoryEntry(
                    timestamp=str(item["timestamp"]),
                    action=str(item["action"]),
                    status=str(item["status"]),
                    summary=str(item["summary"]),
                    reclaimed=max(0, int(item.get("reclaimed", 0))),
                    details=item.get("details") if isinstance(item.get("details"), dict) else None,
                )
            )
        except (KeyError, TypeError, ValueError):
            continue
    return entries[-max(0, limit) :][::-1]


def add_entry(
    action: str,
    status: str,
    summary: str,
    *,
    reclaimed: int = 0,
    details: dict[str, Any] | None = None,
) -> HistoryEntry:
    entry = HistoryEntry(
        timestamp=datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        action=action,
        status=status,
        summary=summary,
        reclaimed=max(0, int(reclaimed)),
        details=details,
    )
    current = list(reversed(list_entries(limit=499)))
    current.append(entry)
    _write(current[-500:])
    return entry


def clear() -> None:
    _write([])


def _write(entries: list[HistoryEntry]) -> None:
    path = history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(
        json.dumps([asdict(item) for item in entries], indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    os.chmod(temp, 0o600)
    temp.replace(path)
