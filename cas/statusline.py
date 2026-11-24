"""`cas statusline`: Claude Code status line command.

Claude Code runs it on every refresh and passes a JSON blob on stdin that includes
rate_limits.five_hour / seven_day for the account the session runs on. cas records that (so
every other cas command knows this account's live usage for free), remembers the session id
(so `cas handoff` can resume it on another account), fetches the other accounts' usage in the
background when it's out of date (it shows on a later refresh), and prints one line:

    work 5h 58% left ↻14:20 · wk 76% · ⟲1 │ next: personal 5h 100% · wk 41%

When no other account has room, it shows the one that frees up first instead of `next:`, also
at this account's limit:

    work 5h 58% left ↻14:20 · wk 76% │ personal 5h 0% ↻15:10 · wk 41% · ⟲
    work 5h 8% left ↻14:20 · wk 76% · ⟲1 │ limit reset available: /limit-reset │ personal 5h 0% ↻15:10 · wk 41%

Near the limit, while this conversation's agents are at work, it says so: they stop when the
account does, so switching once they're done interrupts nothing. At the limit it says how many
stopped; `cas handoff` has Claude continue them on the next account.

When this account's login is in the locked macOS Keychain (common over SSH), the session can't
renew it and will ask for /login once its token expires: the line says so while there is time
to unlock it.

If a status line was configured before cas, it still runs; its output comes first.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import agents as subagents
from . import keychain, picker, resets
from .config import Config, Profile
from .usage import (
    best_snapshot,
    effective,
    load_state,
    record_snapshot,
    save_state,
    snapshot_from_api,
)

WRITE_EVERY_S = 30
KEYCHAIN_CHECK_EVERY_S = 60


def _c(code: str, s: str) -> str:
    return f"\033[{code}m{s}\033[0m"


def _left_color(left: float) -> str:
    return "32" if left > 40 else ("33" if left > 15 else "31")


def _hm(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts + 30)) if ts else ""  # to the nearest minute


def _week(e: Any, c: Any) -> Optional[str]:
    """What's left of the weekly limit: dim, coloured once it runs low."""
    if e.week_pct is None:
        return None
    wl = max(0.0, 100 - e.week_pct)
    return c(_left_color(wl) if wl <= 40 else "2", f"wk {wl:.0f}%")


def _old(sec: float) -> str:
    return f"{int(sec // 60)}m old" if sec < 5400 else f"{int(sec // 3600)}h old"


def refresh_others(cfg: Config, p: Profile) -> None:
    """Keep the other accounts' usage current during the session (this one's comes on stdin)."""
    from .cli import refresh_in_background  # cli imports this module lazily: no cycle

    refresh_in_background(cfg, skip=[p.name])


def _record(p: Profile, data: Dict[str, Any], now: float) -> None:
    st = load_state(p.name)
    changed = False
    rl = data.get("rate_limits")
    if isinstance(rl, dict):
        snap = snapshot_from_api(rl, "statusline", now, p.account_uuid)
        if snap is not None:
            old = st.get("usage") or {}
            same = (old.get("five_hour") == snap.to_dict()["five_hour"]
                    and old.get("seven_day") == snap.to_dict()["seven_day"])
            if not same or now - float(old.get("fetched_at", 0)) > WRITE_EVERY_S:
                record_snapshot(p.name, snap)
                st = load_state(p.name)
    sid = data.get("session_id")
    if isinstance(sid, str) and sid:
        ws = data.get("workspace") or {}
        cwd = ws.get("current_dir") or ws.get("project_dir") or data.get("cwd") or os.getcwd()
        last = st.get("last_session") or {}
        if last.get("id") != sid or last.get("cwd") != cwd or now - float(last.get("ts", 0)) > WRITE_EVERY_S:
            st["last_session"] = {"id": sid, "cwd": cwd, "ts": now,
                                  "transcript_path": data.get("transcript_path")}
            # which account each recent conversation ran on (handoff needs the source account)
            recent = st.get("recent_sessions") if isinstance(st.get("recent_sessions"), dict) else {}
            recent[sid] = now
            st["recent_sessions"] = dict(sorted(recent.items(), key=lambda kv: kv[1])[-100:])
            changed = True
    if changed:
        save_state(p.name, st)


def login_stranded(p: Profile, now: float) -> bool:
    """The session's login is in the locked Keychain (checked at most once a minute)."""
    st = load_state(p.name)
    last = st.get("keychain_check") if isinstance(st.get("keychain_check"), dict) else {}
    if now - float(last.get("at", 0)) < KEYCHAIN_CHECK_EVERY_S:
        return bool(last.get("stranded"))
    login = keychain.check(p)
    stranded = bool(login and login.stranded)
    st = load_state(p.name)
    st["keychain_check"] = {"at": now, "stranded": stranded}
    save_state(p.name, st)
    return stranded


def started_outside(cfg: Config, p: Optional[Profile], env: Dict[str, str]) -> Optional[Profile]:
    """The account cas is on, when this session was started without cas on another one.

    cas sets CAS_PROFILE on every session it starts; a session without it or CLAUDE_CONFIG_DIR ran
    the claude binary directly, e.g. from a terminal opened before `cas init` added the shell
    function, so it landed on ~/.claude whatever account cas is using.
    """
    if p is None or env.get("CAS_PROFILE") or env.get("CLAUDE_CONFIG_DIR"):
        return None
    from .cli import active_profile  # cli imports this module lazily: no cycle

    want = active_profile(cfg)
    return want if want.name != p.name else None


def outside_fix(env: Dict[str, str], transcript: Any) -> str:
    """One command, run after exiting, that fixes this shell and reopens the conversation on cas's account."""
    from .cli import shell_hook_rc

    rc = shell_hook_rc(env.get("SHELL"))
    if rc is not None:
        first = f"source ~/{rc.name}"
    else:
        first = "cas init && source ~/" + (".bashrc" if os.path.basename(env.get("SHELL", "")) == "bash"
                                            else ".zshrc")
    # nothing to carry over until the first message has written the transcript
    empty = isinstance(transcript, str) and transcript and not os.path.exists(transcript)
    return f"{first} && claude" + ("" if empty else " -c")


def session_agents(data: Any) -> List[subagents.Agent]:
    tp = data.get("transcript_path") if isinstance(data, dict) else None
    if not isinstance(tp, str) or not tp:
        return []
    return subagents.session_agents(Path(tp))


def near_limit(e: Any, settings: Dict[str, Any]) -> bool:
    margin = float(settings.get("agent_warning", 15) or 0)
    return margin > 0 and e.five_pct is not None and \
        e.five_pct >= float(settings.get("switch_threshold", 90)) - margin


def _agents(n: int) -> str:
    return f"{n} agent{'s' if n != 1 else ''}"


def waiting(cfg: Config, b: picker.Blocked, now: float, c: Any) -> str:
    """The other account when none has room (the one free soonest): what's left and when it frees up."""
    from .cli import when  # cli imports this module lazily: no cycle

    e = b.eff
    if e.five_pct is not None and e.five_pct >= float(cfg.settings.get("switch_threshold", 90)):
        label, pct, at = "5h", e.five_pct, e.five_resets
    elif e.week_pct is not None and e.week_pct >= float(cfg.settings.get("weekly_threshold", 97)):
        label, pct, at = "wk", e.week_pct, e.week_resets
    else:
        return c("2", f" │ {b.profile.name} {b.reason}")  # not logged in
    left = max(0.0, 100 - pct)
    out = c("2", f" │ {b.profile.name} ") + c(_left_color(left), f"{label} {left:.0f}%")
    if at:
        out += c("2", f" ↻{when(at, now)}")
    wk = _week(e, c) if label == "5h" else None  # stopped by the week: that's the figure above
    if wk:
        out += c("2", " · ") + wk
    rs = resets.best_status(b.profile, now)
    n = rs.count(now) if rs else None
    if n or (rs and rs.has_any(now)):
        out += c("2", " · ") + c("36", f"⟲{n or ''}")  # a reset there would free it now: `cas reset`
    if e.age is not None and e.age > 2 * float(cfg.settings.get("live_max_age_s", 300)):
        out += c("2", f" ({_old(e.age)})")
    return out


def render(cfg: Config, p: Optional[Profile], now: Optional[float] = None, color: bool = True,
           keychain_locked: bool = False, outside: Optional[Profile] = None, fix: str = "",
           agents: Sequence[subagents.Agent] = ()) -> str:
    now = time.time() if now is None else now
    c = _c if color else (lambda _code, s: s)
    if p is None:
        return c("2", "cas: this config dir is not a cas profile")
    e = effective(best_snapshot(p), now)
    parts = [c("1", p.name)]
    if e.five_pct is None:
        parts.append(c("2", "5h ?"))
    else:
        left = max(0.0, 100 - e.five_pct)
        txt = f"5h {left:.0f}% left" + (f" ↻{_hm(e.five_resets)}" if e.five_resets else "")
        parts.append(c(_left_color(left), txt))
    wk = _week(e, c)
    if wk:
        parts.append(wk)
    rs = resets.best_status(p, now)
    n = rs.count(now) if rs else None
    if n:
        parts.append(c("36", f"⟲{n}"))
    elif rs and rs.has_any(now):
        parts.append(c("36", "⟲"))
    line = " ".join(parts[:2]) + "".join(f" · {x}" for x in parts[2:])

    items = [(q, effective(best_snapshot(q), now)) for q in cfg.profile_list()]
    ordered, blocked = picker.rank(items, cfg.settings, exclude={p.name})
    alt = ordered[0] if ordered else None
    other = waiting(cfg, blocked[0], now, c) if alt is None and blocked else ""  # none has room
    exhausted = picker.exhausted(e, cfg.settings)
    running = subagents.running(agents, now) if not exhausted and near_limit(e, cfg.settings) else []
    if exhausted:
        if alt:
            stopped = subagents.cut_off(agents, now - subagents.HANDOFF_WINDOW_S) \
                if cfg.settings.get("resume_agents", True) else []
            line += " │ " + c("33", f"limit: exit, then `cas handoff` → {alt.profile.name}" +
                              (f", which continues the {_agents(len(stopped))}" if stopped else ""))
        elif n or (rs and rs.has_any(now)):
            line += " │ " + c("36", "limit reset available: /limit-reset")
        elif other:  # it shows when the other one resets
            line += " │ " + c("31", "all accounts at limit")
        else:
            soon = min((x.five_resets for _, x in items if x.five_resets), default=None)
            line += " │ " + c("31", "all accounts at limit" + (f" · first reset {_hm(soon)}" if soon else ""))
    elif outside is not None:
        line += " │ " + c("33", f"⚠ opened outside cas, which is on {outside.name} → exit, then: {fix}")
    elif running:
        them = "them" if len(running) != 1 else "it"
        line += " │ " + c("33", f"⚠ {_agents(len(running))} running, limit near" +
                          (f" → let {them} finish, then exit and `cas handoff` → {alt.profile.name}" if alt
                           else ", no other account has room"))
    elif alt:
        left = "5h ?" if alt.eff.five_pct is None else f"5h {max(0.0, 100 - alt.eff.five_pct):.0f}%"
        old = alt.eff.age is not None and alt.eff.age > 2 * float(cfg.settings.get("live_max_age_s", 300))
        wk = _week(alt.eff, c)
        line += c("2", f" │ next: {alt.profile.name} {left}") + (c("2", " · ") + wk if wk else "") + \
            (c("2", f" ({_old(alt.eff.age)})") if old else "")
    line += other
    if keychain_locked:
        line += " │ " + c("31", "⚠ Keychain locked: this login can't renew → security unlock-keychain")
    return line


def run_chain(cmd: str, raw: str) -> str:
    try:
        r = subprocess.run(cmd, shell=True, input=raw, capture_output=True, text=True, timeout=3)
        return r.stdout.rstrip("\n")
    except (OSError, subprocess.SubprocessError):
        return ""


def main(raw: str, cfg: Optional[Config] = None, env: Optional[Dict[str, str]] = None) -> str:
    cfg = cfg or Config.load()
    now = time.time()
    try:
        data = json.loads(raw) if raw.strip() else {}
    except ValueError:
        data = {}
    p = cfg.current(env)
    if p is not None and isinstance(data, dict):
        try:
            _record(p, data, now)
        except OSError:
            pass
    try:
        locked = p is not None and login_stranded(p, now)
    except OSError:
        locked = False
    env = os.environ if env is None else env
    try:
        outside = started_outside(cfg, p, env)
        fix = outside_fix(env, data.get("transcript_path") if isinstance(data, dict) else None) if outside else ""
    except OSError:
        outside, fix = None, ""
    try:
        found = session_agents(data)
    except Exception:
        found = []  # a status line must never break over this
    ours = render(cfg, p, now, keychain_locked=locked, outside=outside, fix=fix, agents=found)
    if p is not None:
        try:
            refresh_others(cfg, p)  # shows on a later refresh, once the fetch is back
        except Exception:
            pass  # a status line must never break over this
    chain = cfg.settings.get("statusline_chain")
    if chain:
        before = run_chain(chain, raw)
        if before:
            first, _, rest = before.partition("\n")
            return f"{first}  {ours}" + (f"\n{rest}" if rest else "")
    return ours
