"""Persistent state: what we've seen, what we've alerted on, monitor health.

Stored as one JSON file on a dedicated `state` branch of the repository, so it
survives between GitHub Actions runs. Only real changes are written. There are
no per-run timestamps, so an unchanged feed produces no commit at all.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

STATE_VERSION = 1
MAX_EVENT_LOG = 400


def empty_state() -> dict[str, Any]:
    return {
        "version": STATE_VERSION,
        "initialized_at": None,
        "baselined_destinations": [],
        "last_sweep_at": None,
        "last_daily_summary": None,
        "health": {
            "consecutive_failures": 0,
            "failure_alert_sent": False,
            "last_error": None,
            "empty_feed_checks": 0,
            "empty_feed_alert_sent": False,
        },
        "rewards": {},
        "events": [],
    }


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists() or path.stat().st_size == 0:
        return empty_state()
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        # Refuse to silently start over: a fresh baseline would hide real changes,
        # and a corrupted file is something the owner should hear about.
        raise RuntimeError(f"State file {path} is unreadable: {e}") from e
    base = empty_state()
    base.update({k: v for k, v in data.items() if k in base})
    base["health"] = {**empty_state()["health"], **(data.get("health") or {})}
    return base


def save_state(path: Path, state: dict[str, Any]) -> bool:
    """Write state atomically. Returns True if the content changed."""
    state["events"] = state.get("events", [])[-MAX_EVENT_LOG:]
    text = json.dumps(state, indent=1, sort_keys=True, ensure_ascii=False) + "\n"
    old = path.read_text(encoding="utf-8") if path.exists() else None
    if old == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True
