"""Limit resets you recorded, per account, shared between machines when `cas share` is on.

Each machine writes only its own file, so two machines never write the same file:
    local:   ~/.claude-accounts/state/_resets.json
    shared:  <shared folder>/resets/<host>.json   (a copy of the local file)

Per account (keyed by account id, the same on every machine):
    set    the last count you recorded (count, kind, expires, set_at)
    uses   resets this machine saw being used, as [before, after] time intervals: the reset
           happened between its last reading before the usage drop and the first one after

Reading merges every machine: the newest `set` wins. Two machines that saw the same reset have
overlapping intervals, so the number of resets used is the largest set of non-overlapping
intervals. Only uses that started after the count was set are subtracted (the count you read on
claude.ai already includes earlier ones).
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import Config, read_json, state_dir, write_json_atomic


def _local_path() -> Path:
    return state_dir() / "_resets.json"


def _load_local() -> Dict[str, Any]:
    d = read_json(_local_path(), {})
    return d if isinstance(d, dict) else {}


def _shared_dir() -> Optional[Path]:
    from . import shared  # local import: shared doesn't depend on this module

    store = shared.store_path(Config.load().settings)
    return store / "resets" if store else None


def publish() -> None:
    """Copy this machine's records into the shared folder (if sharing is on)."""
    from .shared import host

    d = _shared_dir()
    local = _load_local()
    if d is None or not local:
        return
    try:
        d.mkdir(parents=True, exist_ok=True)
        write_json_atomic(d / f"{host()}.json", local, 0o644)
    except OSError:
        pass  # the folder may be offline; the next sync publishes again


def _save_local(data: Dict[str, Any]) -> None:
    write_json_atomic(_local_path(), data, 0o600)
    publish()


def set_count(account: str, count: int, kind: str = "full", ends_at: Optional[float] = None) -> None:
    data = _load_local()
    entry = data.setdefault(account, {})
    entry["set"] = {"count": max(0, int(count)), "kind": kind, "ends_at": ends_at, "set_at": time.time()}
    _save_local(data)


def note_use(account: str, before: float, after: float) -> None:
    data = _load_local()
    data.setdefault(account, {}).setdefault("uses", []).append([float(before), float(after)])
    _save_local(data)


def _distinct(intervals: List[List[float]]) -> int:
    """Largest number of pairwise non-overlapping intervals (greedy by end time)."""
    n, last_end = 0, float("-inf")
    for start, end in sorted(intervals, key=lambda iv: iv[1]):
        if start > last_end:
            n, last_end = n + 1, end
    return n


def merged(account: Optional[str]) -> Optional[Dict[str, Any]]:
    """{"left", "count", "kind", "ends_at", "set_at", "used"} across machines, or None."""
    if not account:
        return None
    entries = [_load_local().get(account)]
    d = _shared_dir()
    if d is not None and d.is_dir():
        from .shared import host

        for f in d.glob("*.json"):
            if f.stem != host():
                data = read_json(f, {})
                entries.append(data.get(account) if isinstance(data, dict) else None)
    entries = [e for e in entries if isinstance(e, dict)]
    sets = [e["set"] for e in entries if isinstance(e.get("set"), dict)]
    if not sets:
        return None
    latest = max(sets, key=lambda s: float(s.get("set_at") or 0))
    since = float(latest.get("set_at") or 0)
    uses = [iv for e in entries for iv in (e.get("uses") or [])
            if isinstance(iv, list) and len(iv) == 2 and float(iv[0]) >= since]
    used = _distinct(uses)
    return {**latest, "used": used, "left": max(0, int(latest.get("count") or 0) - used)}
