"""Limit resets: grants that clear a usage limit early.

Claude Code offers them through its own /limit-reset command, which asks for confirmation.
cas only reads their status and reports what you have; it never claims a reset itself. When
you choose to use one, cas opens Claude Code on that account with /limit-reset, so the use
goes through Claude Code's confirmation dialog.

Sources:
  live    `cedar_ember` block of GET /api/oauth/usage?cedar_ember=1 (grants, counts, expiry).
          The server shares the details only with some clients: to cas it may answer
          "ineligible: surface", which means "not telling you", not "you have none".
  claude  `unusedLimitResetStatusCache` in the profile's .claude.json (only "holds a reset: yes/no")
  flags   Claude Code's cached feature flags: without `tengu_cedar_ember` the account isn't
          offered limit resets at all (and /limit-reset is not available).
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .config import Profile, read_json
from .usage import load_state, parse_time, save_state

KIND_NAMES = {"session": "5-hour reset", "weekly": "weekly reset", "full": "full reset"}
# Argument Claude Code's /limit-reset accepts to preselect a kind.
KIND_ARG = {"session": "5-hour", "weekly": "weekly", "full": "full"}
WINDOW_NAMES = {"five_hour": "5-hour", "seven_day": "weekly", "seven_day_opus": "Opus weekly",
                "seven_day_sonnet": "Sonnet weekly"}
FEATURE_FLAG = "tengu_cedar_ember"
# Ineligibility reasons that describe the asking client, not the account.
UNKNOWN_REASONS = {"surface", "cli_version", "mobile", "unavailable", "unknown"}


@dataclass
class Grant:
    id: str
    kind: str
    label: str = ""
    left: int = 0
    total: int = 0
    starts_at: Optional[float] = None
    ends_at: Optional[float] = None
    clears: List[str] = field(default_factory=list)
    usable_now: bool = False
    requires_limit: bool = True
    paused: bool = False
    unavailable_reason: Optional[str] = None

    @property
    def name(self) -> str:
        return self.label or KIND_NAMES.get(self.kind, f"{self.kind} reset")

    def live(self, now: float) -> bool:
        return self.left > 0 and not self.paused and (self.ends_at is None or self.ends_at > now)


@dataclass
class ResetStatus:
    fetched_at: float
    source: str
    eligible: Optional[bool] = None
    ineligible_reason: Optional[str] = None
    grants: List[Grant] = field(default_factory=list)
    at_limit: bool = False
    cooldown_until: Optional[float] = None
    holds_unused: Optional[bool] = None
    valid_until: Optional[float] = None
    feature: Optional[bool] = None  # limit resets offered to this account (Claude Code's feature flags)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @property
    def informative(self) -> bool:
        """False when the answer says nothing about the account (e.g. 'ineligible: surface')."""
        if self.source == "live" and self.eligible is False:
            return (self.ineligible_reason or "unknown") not in UNKNOWN_REASONS
        if self.source == "flags":
            return self.feature is False
        return True

    def maybe(self, now: Optional[float] = None) -> bool:
        """Resets may exist but cas can't see them: worth checking with /limit-reset in Claude Code."""
        return self.feature is not False and not self.informative and self.has_any(now) is None

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Optional["ResetStatus"]:
        try:
            grants = [Grant(**g) for g in d.get("grants") or []]
            return cls(**{**d, "grants": grants})
        except (TypeError, ValueError):
            return None

    def usable(self, now: Optional[float] = None) -> List[Grant]:
        now = time.time() if now is None else now
        return sorted((g for g in self.grants if g.live(now)), key=lambda g: g.ends_at or float("inf"))

    def count(self, now: Optional[float] = None) -> Optional[int]:
        """Resets left; None when unknown or only the yes/no cache is known."""
        if self.grants or (self.source == "live" and self.informative):
            return sum(g.left for g in self.usable(now))
        if self.feature is False:
            return 0
        return None

    def has_any(self, now: Optional[float] = None) -> Optional[bool]:
        c = self.count(now)
        if c is not None:
            return c > 0
        return self.holds_unused

    def summary(self, now: Optional[float] = None, short: bool = False) -> str:
        now = time.time() if now is None else now
        if self.feature is False and not self.grants:
            return "-" if short else "not offered to this account"
        if self.source == "live" and self.eligible is False:
            if not self.informative:
                return "?" if short else ("unknown: the server shares reset details only with Claude Code "
                                          "itself (`cas reset` opens /limit-reset there)")
            return "-" if short else f"none (not offered: {self.ineligible_reason})"
        grants = self.usable(now)
        if grants:
            first = grants[0]
            parts = []
            for kind in sorted({g.kind for g in grants}):
                n = sum(g.left for g in grants if g.kind == kind)
                parts.append(f"{n} {KIND_NAMES.get(kind, kind)}" + ("s" if n > 1 else ""))
            exp = f" · use by {_when(first.ends_at, now)}" if first.ends_at else ""
            return (f"{sum(g.left for g in grants)}" + (f" ↦{_when(first.ends_at, now)}" if first.ends_at else "")) \
                if short else ", ".join(parts) + exp
        if self.count(now) == 0:
            return "0" if short else "none left"
        if self.holds_unused:
            return "yes" if short else "at least one (run `cas status --live` for details)"
        if self.holds_unused is False:
            return "0" if short else "none"
        return "?" if short else "unknown"


def _when(ts: Optional[float], now: float) -> str:
    if not ts:
        return "?"
    lt = time.localtime(ts)
    if ts - now < 20 * 3600 and lt.tm_yday == time.localtime(now).tm_yday:
        return time.strftime("%H:%M", lt)
    return time.strftime("%b %d", lt).replace(" 0", " ")


def _kind(g: Dict[str, Any]) -> str:
    t = g.get("type")
    if t in KIND_NAMES:
        return t
    clears = set(g.get("clears") or [])
    if "five_hour" in clears and "seven_day" in clears:
        return "full"
    if "seven_day" in clears:
        return "weekly"
    if "five_hour" in clears:
        return "session"
    return str(t or "other")


def parse_status(block: Any, fetched_at: Optional[float] = None, source: str = "live") -> Optional[ResetStatus]:
    """Parse the `cedar_ember` block of /api/oauth/usage?cedar_ember=1."""
    if not isinstance(block, dict) or "eligible" not in block:
        return None
    grants: List[Grant] = []
    for g in block.get("grants") or []:
        if not isinstance(g, dict) or not isinstance(g.get("id"), str):
            continue
        try:
            left = int(g.get("resets_left", 0))
        except (TypeError, ValueError):
            continue
        grants.append(Grant(
            id=g["id"],
            kind=_kind(g),
            label=str(g.get("label") or ""),
            left=max(0, left),
            total=int(g.get("resets_total") or 0) if str(g.get("resets_total", "0")).isdigit() else 0,
            starts_at=parse_time(g.get("starts_at")),
            ends_at=parse_time(g.get("ends_at")),
            clears=[c for c in g.get("clears") or [] if isinstance(c, str)],
            usable_now=bool(g.get("usable_now", False)),
            requires_limit=bool(g.get("use_requires_limit", True)),
            paused=bool(g.get("paused", False)),
            unavailable_reason=g.get("unavailable_reason"),
        ))
    return ResetStatus(
        fetched_at=fetched_at or time.time(),
        source=source,
        eligible=bool(block.get("eligible")),
        ineligible_reason=block.get("ineligible_reason"),
        grants=grants,
        at_limit=bool(block.get("at_limit", False)),
        cooldown_until=parse_time(block.get("cooldown_until")),
    )


def from_claude_cache(profile: Profile) -> Optional[ResetStatus]:
    data = read_json(profile.global_json, {}) or {}
    c = data.get("unusedLimitResetStatusCache") if isinstance(data, dict) else None
    if not isinstance(c, dict):
        return None
    acct = (data.get("oauthAccount") or {}).get("accountUuid")
    if c.get("accountUuid") and acct and c["accountUuid"] != acct:
        return None
    return ResetStatus(
        fetched_at=parse_time(c.get("checkedAt")) or 0.0,
        source="claude",
        holds_unused=bool(c.get("holdsUnusedReset")),
        valid_until=parse_time(c.get("validUntil")),
    )


def feature_enabled(profile: Profile) -> Optional[bool]:
    """Is the limit-reset feature on for this account, per Claude Code's cached feature flags?"""
    data = read_json(profile.global_json, {}) or {}
    if not isinstance(data, dict):
        return None
    seen_any = False
    for key in ("cachedGrowthBookFeatures", "cachedFeatureFlags"):
        flags = data.get(key)
        if not isinstance(flags, dict) or not flags:
            continue
        seen_any = True
        v = flags.get(FEATURE_FLAG)
        if v is None:
            continue
        if isinstance(v, dict):
            return v.get("enabled", True) is not False
        return bool(v)
    return False if seen_any else None


def record(name: str, status: ResetStatus) -> None:
    st = load_state(name)
    st["resets"] = status.to_dict()
    save_state(name, st)


def best_status(profile: Profile, now: Optional[float] = None) -> Optional[ResetStatus]:
    now = time.time() if now is None else now
    cands: List[ResetStatus] = []
    st = load_state(profile.name)
    if isinstance(st.get("resets"), dict):
        s = ResetStatus.from_dict(st["resets"])
        if s:
            cands.append(s)
    c = from_claude_cache(profile)
    if c and (c.valid_until is None or c.valid_until > now):
        cands.append(c)
    feature = feature_enabled(profile) if profile.logged_in() else None
    good = [s for s in cands if s.informative]
    if good:
        best = max(good, key=lambda s: s.fetched_at)
    elif feature is False:
        best = ResetStatus(fetched_at=now, source="flags", feature=False)
    elif cands:
        best = max(cands, key=lambda s: s.fetched_at)
    elif feature:
        best = ResetStatus(fetched_at=now, source="flags", feature=True)
    else:
        return None
    best.feature = feature if best.feature is None else best.feature
    return best


def limit_reset_arg(grant: Optional[Grant]) -> List[str]:
    if grant is None:
        return ["/limit-reset"]
    arg = KIND_ARG.get(grant.kind)
    return [f"/limit-reset {arg}"] if arg else ["/limit-reset"]
