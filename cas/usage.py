"""Per-account usage: where it comes from, how fresh it is, and what it means right now.

Sources, freshest wins:
  statusline  Claude Code passes rate_limits.five_hour/seven_day to the status line command on
              every refresh; `cas statusline` records them for the profile it runs under.
  claude      Claude Code caches the /api/oauth/usage response as `cachedUsageUtilization`
              in each config dir's .claude.json.
  live        cas calls GET /api/oauth/usage itself with the profile's OAuth token (read-only;
              the token is read from the keychain and never printed or stored).
  ping        after a priming ping, cas records that a window just opened.

A window whose reset time has passed is treated as reset (0%), so an account idle for
5+ hours shows as fresh even if nothing refreshed its data.
"""

from __future__ import annotations

import json
import re
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

from . import __version__
from .config import Profile, keychain_account, read_json, state_dir, write_json_atomic

FIVE_HOURS = 5 * 3600
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"


# --------------------------------------------------------------------------- parsing

def parse_time(v: Any) -> Optional[float]:
    """ISO-8601 string, epoch seconds or epoch milliseconds -> epoch seconds."""
    if v is None or v == "" or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        return x / 1000.0 if x > 1e11 else x
    if isinstance(v, str):
        s = v.strip()
        if re.fullmatch(r"\d+(\.\d+)?", s):
            return parse_time(float(s))
        s = s.replace("Z", "+00:00").replace("z", "+00:00")
        m = re.match(r"^(.*T\d{2}:\d{2}:\d{2})(\.\d+)?(.*)$", s)
        if m:
            frac = m.group(2)
            frac = (frac[:7]).ljust(7, "0") if frac else ""
            s = m.group(1) + frac + m.group(3)
        try:
            dt = datetime.fromisoformat(s)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    return None


@dataclass
class Window:
    pct: Optional[float] = None
    resets_at: Optional[float] = None


@dataclass
class Snapshot:
    fetched_at: float
    source: str
    five_hour: Window = field(default_factory=Window)
    seven_day: Window = field(default_factory=Window)
    account_uuid: Optional[str] = None
    # Extra usage ("usage credits" that cover you past the plan limits, if you enable them):
    # {"enabled": bool, "limit": float, "used": float, "currency": "USD", "disabled_reason": str|None}
    extra: Optional[Dict[str, Any]] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> Optional["Snapshot"]:
        try:
            return cls(
                fetched_at=float(d["fetched_at"]),
                source=str(d.get("source", "?")),
                five_hour=Window(**(d.get("five_hour") or {})),
                seven_day=Window(**(d.get("seven_day") or {})),
                account_uuid=d.get("account_uuid"),
                extra=d.get("extra") if isinstance(d.get("extra"), dict) else None,
            )
        except (KeyError, TypeError, ValueError):
            return None


def parse_extra_usage(d: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(d, dict):
        return None
    try:
        scale = 10 ** int(d.get("decimal_places") or 0)
        limit = d.get("monthly_limit")
        used = d.get("used_credits")
        return {
            "enabled": bool(d.get("is_enabled")),
            "limit": float(limit) / scale if isinstance(limit, (int, float)) else None,
            "used": float(used) / scale if isinstance(used, (int, float)) else None,
            "currency": d.get("currency") or "USD",
            "disabled_reason": d.get("disabled_reason"),
        }
    except (TypeError, ValueError):
        return None


def extra_summary(extra: Optional[Dict[str, Any]]) -> str:
    if not extra:
        return "?"
    sym = "$" if extra.get("currency") == "USD" else f"{extra.get('currency')} "
    if not extra.get("enabled"):
        why = extra.get("disabled_reason")
        return "off" + (f" ({why.replace('_', ' ')})" if why else "")
    limit, used = extra.get("limit"), extra.get("used")
    if limit is None:
        return "on"
    return f"{sym}{max(0.0, limit - (used or 0)):.2f} of {sym}{limit:.2f} left"


def _window(d: Any) -> Optional[Window]:
    if not isinstance(d, dict):
        return None
    pct = d.get("utilization", d.get("used_percentage"))
    pct = float(pct) if isinstance(pct, (int, float)) and not isinstance(pct, bool) else None
    return Window(pct, parse_time(d.get("resets_at")))


def snapshot_from_api(data: Any, source: str, fetched_at: Optional[float] = None,
                      account_uuid: Optional[str] = None) -> Optional[Snapshot]:
    """Shape of /api/oauth/usage (and of the status line's rate_limits object)."""
    if not isinstance(data, dict):
        return None
    five, seven = _window(data.get("five_hour")), _window(data.get("seven_day"))
    if five is None and seven is None:
        return None
    return Snapshot(fetched_at or time.time(), source, five or Window(), seven or Window(), account_uuid,
                    parse_extra_usage(data.get("extra_usage")))


def snapshot_from_claude_cache(profile: Profile) -> Optional[Snapshot]:
    data = read_json(profile.global_json, {}) or {}
    cached = data.get("cachedUsageUtilization") if isinstance(data, dict) else None
    if not isinstance(cached, dict):
        return None
    acct = (data.get("oauthAccount") or {}).get("accountUuid")
    if cached.get("accountUuid") and acct and cached["accountUuid"] != acct:
        return None  # cache belongs to a previous login
    fetched = parse_time(cached.get("fetchedAtMs"))
    return snapshot_from_api(cached.get("utilization"), "claude", fetched, cached.get("accountUuid") or acct)


# --------------------------------------------------------------------------- state

def state_path(name: str):
    return state_dir() / f"{name}.json"


def load_state(name: str) -> Dict[str, Any]:
    st = read_json(state_path(name), {})
    return st if isinstance(st, dict) else {}


def save_state(name: str, st: Dict[str, Any]) -> None:
    write_json_atomic(state_path(name), st, 0o600)


def update_state(name: str, **kv: Any) -> Dict[str, Any]:
    st = load_state(name)
    st.update(kv)
    save_state(name, st)
    return st


def record_snapshot(name: str, snap: Snapshot) -> None:
    st = load_state(name)
    old = Snapshot.from_dict(st["usage"]) if isinstance(st.get("usage"), dict) else None
    if old is not None and old.fetched_at > snap.fetched_at:
        return
    if snap.extra is None and old is not None and old.extra is not None:
        snap.extra = old.extra  # the status line doesn't carry credits; keep the last known
    if old is not None and reset_used(old, snap):
        st.setdefault("resets_used", []).append(snap.fetched_at)
        m = st.get("resets_manual")
        if isinstance(m, dict):
            m["left"] = max(0, int(m.get("left") or 0) - 1)
            if not m["left"]:
                st.pop("resets_manual")
    st["usage"] = snap.to_dict()
    save_state(name, st)


def reset_used(old: Snapshot, new: Snapshot) -> bool:
    """Usage fell sharply while its window was still open: a limit reset was used."""
    for o, n in ((old.five_hour, new.five_hour), (old.seven_day, new.seven_day)):
        still_open = o.resets_at is not None and o.resets_at - new.fetched_at > 600
        if still_open and o.pct is not None and n.pct is not None and n.pct + 15 <= o.pct:
            return True
    return False


def best_snapshot(profile: Profile) -> Optional[Snapshot]:
    candidates = []
    st = load_state(profile.name)
    if isinstance(st.get("usage"), dict):
        s = Snapshot.from_dict(st["usage"])
        if s:
            candidates.append(s)
    c = snapshot_from_claude_cache(profile)
    if c:
        candidates.append(c)
    acct = profile.account_uuid
    if acct:
        candidates = [s for s in candidates if not s.account_uuid or s.account_uuid == acct]
    if not candidates:
        return None
    candidates.sort(key=lambda s: s.fetched_at, reverse=True)
    best = candidates[0]
    if best.extra is None:  # the status line carries no credit info; borrow the latest known
        best.extra = next((s.extra for s in candidates if s.extra is not None), None)
    return best


# --------------------------------------------------------------------------- effective view

@dataclass
class Effective:
    five_pct: Optional[float] = None
    five_resets: Optional[float] = None
    week_pct: Optional[float] = None
    week_resets: Optional[float] = None
    window: str = "unknown"  # active | idle | unknown
    age: Optional[float] = None
    source: Optional[str] = None
    estimated: bool = False

    @property
    def headroom(self) -> Optional[float]:
        return None if self.five_pct is None else max(0.0, 100.0 - self.five_pct)


def effective(snap: Optional[Snapshot], now: Optional[float] = None) -> Effective:
    now = time.time() if now is None else now
    if snap is None:
        return Effective()
    e = Effective(age=max(0.0, now - snap.fetched_at), source=snap.source)
    f, w = snap.five_hour, snap.seven_day
    if f.resets_at is not None and f.resets_at <= now:
        e.five_pct, e.five_resets, e.window, e.estimated = 0.0, None, "idle", True
    elif f.resets_at is not None:
        e.five_pct, e.five_resets, e.window = f.pct, f.resets_at, "active"
    elif f.pct is not None:
        e.five_pct, e.window = f.pct, ("idle" if f.pct <= 0 else "active")
    if w.resets_at is not None and w.resets_at <= now:
        e.week_pct, e.week_resets, e.estimated = 0.0, None, True
    else:
        e.week_pct, e.week_resets = w.pct, w.resets_at
    return e


# --------------------------------------------------------------------------- live fetch

def read_oauth(profile: Profile) -> Optional[Dict[str, Any]]:
    """The profile's claudeAiOauth credentials, or None. Never logged or persisted by cas."""
    raw = None
    try:
        r = subprocess.run(
            ["security", "find-generic-password", "-a", keychain_account(), "-w", "-s", profile.keychain_service],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            raw = r.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raw = None
    data = None
    if raw:
        try:
            data = json.loads(raw)
        except ValueError:
            try:  # stored hex-encoded
                data = json.loads(bytes.fromhex(raw).decode("utf-8"))
            except ValueError:
                data = None
    if data is None:  # plaintext fallback Claude Code uses when no keychain is available
        data = read_json(profile.dir / ".credentials.json", None)
    if not isinstance(data, dict):
        return None
    oauth = data.get("claudeAiOauth")
    return oauth if isinstance(oauth, dict) else None


def fetch_live(profile: Profile, timeout: float = 8.0) -> Tuple[Optional[Snapshot], Optional[str]]:
    oauth = read_oauth(profile)
    if not oauth or not oauth.get("accessToken"):
        return None, "not logged in"
    exp = parse_time(oauth.get("expiresAt"))
    if exp is not None and exp < time.time() + 60:
        return None, "token expired (Claude Code refreshes it next time it runs on this account)"
    headers = {
        "Authorization": f"Bearer {oauth['accessToken']}",
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": f"claude-account-switcher/{__version__}",
    }
    data, err, asked_resets = None, None, False
    # `cedar_ember=1` adds the limit-reset status block; fall back to the plain call if refused.
    for url in (USAGE_URL + "?cedar_ember=1", USAGE_URL):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as resp:
                data = json.load(resp)
            asked_resets = "cedar_ember" in url
            break
        except urllib.error.HTTPError as e:
            err = f"usage endpoint answered HTTP {e.code}"
            if e.code in (401, 403):
                break
        except (urllib.error.URLError, OSError, ValueError) as e:
            err = f"usage endpoint unreachable ({e.__class__.__name__})"
            break
    if data is None:
        return None, err
    now = time.time()
    snap = snapshot_from_api(data, "live", now, profile.account_uuid)
    if snap is None:
        return None, "usage endpoint returned no plan limits"
    record_snapshot(profile.name, snap)
    if asked_resets and isinstance(data, dict) and "cedar_ember" in data:
        from . import resets  # local import: resets depends on this module

        status = resets.parse_status(data.get("cedar_ember"), now, "live")
        if status is None and data.get("cedar_ember") is None:
            status = resets.ResetStatus(fetched_at=now, source="live", eligible=False,
                                        ineligible_reason="none offered")
        if status is not None:
            resets.record(profile.name, status)
    return snap, None


def refresh(profile: Profile, settings: Dict[str, Any], live: Optional[bool] = None
            ) -> Tuple[Optional[Snapshot], Optional[str]]:
    """Best snapshot, fetching live data when settings (or `live`) say so."""
    snap = best_snapshot(profile)
    mode = settings.get("live_usage", "auto")
    if live is None:
        live = mode == "always" or (
            mode == "auto" and (snap is None or time.time() - snap.fetched_at > settings.get("live_max_age_s", 300))
        )
    if not live or not profile.logged_in():
        return snap, None
    fresh, err = fetch_live(profile)
    return (fresh or snap), err
