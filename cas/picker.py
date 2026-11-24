"""Choose which account to use next.

Rule: spend the quota that expires soonest. An account whose 5-hour window is already open
loses whatever it doesn't use at reset, so it goes first (soonest reset first) as long as it
has real headroom left. Windows that reset in the same minute are a tie (readings differ by
fractions of a second), and a tie keeps the account you're on, so your machines settle on one. Accounts with no open window keep their quota until you start them,
so they come next, lowest weekly usage first. Accounts over the thresholds are skipped.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .config import Profile
from .usage import Effective

TIER_OPEN = 0      # open window with headroom: use it before it resets
TIER_IDLE = 1      # no open window: quota is safe until started
TIER_UNKNOWN = 2   # no data yet
TIER_LOW = 3       # open window, below threshold but little left


@dataclass
class Choice:
    profile: Profile
    eff: Effective
    tier: int
    reason: str


@dataclass
class Blocked:
    profile: Profile
    eff: Effective
    reason: str
    until: Optional[float]


def _hhmm(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts + 30)) if ts else "?"  # to the nearest minute


def rank(items: Sequence[Tuple[Profile, Effective]], settings: Dict[str, Any],
         exclude: Iterable[str] = (), prefer: Optional[str] = None) -> Tuple[List[Choice], List[Blocked]]:
    thr = float(settings.get("switch_threshold", 90))
    wthr = float(settings.get("weekly_threshold", 97))
    min_head = float(settings.get("min_headroom", 15))
    excluded = set(exclude)
    choices: List[Tuple[tuple, Choice]] = []
    blocked: List[Blocked] = []

    for order, (p, e) in enumerate(items):
        if p.name in excluded or not p.enabled:
            continue
        if not p.logged_in():
            blocked.append(Blocked(p, e, "not logged in", None))
            continue
        if e.five_pct is not None and e.five_pct >= thr:
            blocked.append(Blocked(p, e, f"5h window at {e.five_pct:.0f}%", e.five_resets))
            continue
        if e.week_pct is not None and e.week_pct >= wthr:
            blocked.append(Blocked(p, e, f"weekly limit at {e.week_pct:.0f}%", e.week_resets))
            continue

        week = e.week_pct if e.week_pct is not None else 50.0
        stay = 0 if p.name == prefer else 1
        if e.window == "active" and e.five_pct is not None and (thr - e.five_pct) >= min_head:
            key = (TIER_OPEN, round(e.five_resets / 60) if e.five_resets else float("inf"), stay, week, order)
            reason = f"open window, {100 - e.five_pct:.0f}% left, resets {_hhmm(e.five_resets)}"
            tier = TIER_OPEN
        elif e.window == "idle":
            key = (TIER_IDLE, week, stay, order)
            reason = "no open window" + (f", weekly {e.week_pct:.0f}%" if e.week_pct is not None else "")
            tier = TIER_IDLE
        elif e.window == "unknown":
            key = (TIER_UNKNOWN, order)
            reason = "no usage data yet"
            tier = TIER_UNKNOWN
        else:
            key = (TIER_LOW, e.five_pct or 0.0, order)
            reason = f"only {100 - (e.five_pct or 0):.0f}% left, resets {_hhmm(e.five_resets)}"
            tier = TIER_LOW
        choices.append((key, Choice(p, e, tier, reason)))

    choices.sort(key=lambda kc: kc[0])
    blocked.sort(key=lambda b: (b.until is None, b.until or 0.0))
    return [c for _, c in choices], blocked


def best(items: Sequence[Tuple[Profile, Effective]], settings: Dict[str, Any],
         exclude: Iterable[str] = (), prefer: Optional[str] = None) -> Optional[Choice]:
    ordered, _ = rank(items, settings, exclude, prefer)
    return ordered[0] if ordered else None


def soonest_unblock(blocked: Sequence[Blocked]) -> Optional[Blocked]:
    timed = [b for b in blocked if b.until]
    return min(timed, key=lambda b: b.until) if timed else None


def exhausted(e: Effective, settings: Dict[str, Any]) -> bool:
    return (e.five_pct is not None and e.five_pct >= float(settings.get("switch_threshold", 90))) or (
        e.week_pct is not None and e.week_pct >= float(settings.get("weekly_threshold", 97))
    )
