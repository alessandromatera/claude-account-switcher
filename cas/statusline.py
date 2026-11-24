"""`cas statusline`: Claude Code status line command.

Claude Code runs it on every refresh and passes a JSON blob on stdin that includes
rate_limits.five_hour / seven_day for the account the session runs on. cas records that (so
every other cas command knows this account's live usage for free), remembers the session id
(so `cas handoff` can resume it on another account), and prints one line:

    work 5h 58% left ↻14:20 · wk 76% · ⟲1 │ next: personal 100%

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
from typing import Any, Dict, Optional

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


def render(cfg: Config, p: Optional[Profile], now: Optional[float] = None, color: bool = True,
           keychain_locked: bool = False, outside: Optional[Profile] = None, fix: str = "") -> str:
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
    if e.week_pct is not None:
        wl = max(0.0, 100 - e.week_pct)
        parts.append(c(_left_color(wl) if wl <= 40 else "2", f"wk {wl:.0f}%"))
    rs = resets.best_status(p, now)
    n = rs.count(now) if rs else None
    if n:
        parts.append(c("36", f"⟲{n}"))
    elif rs and rs.has_any(now):
        parts.append(c("36", "⟲"))
    line = " ".join(parts[:2]) + "".join(f" · {x}" for x in parts[2:])

    items = [(q, effective(best_snapshot(q), now)) for q in cfg.profile_list()]
    alt = picker.best(items, cfg.settings, exclude={p.name})
    if picker.exhausted(e, cfg.settings):
        if alt:
            line += " │ " + c("33", f"limit: exit, then `cas handoff` → {alt.profile.name}")
        elif n or (rs and rs.has_any(now)):
            line += " │ " + c("36", "limit reset available: /limit-reset")
        else:
            soon = min((x.five_resets for _, x in items if x.five_resets), default=None)
            line += " │ " + c("31", "all accounts at limit" + (f" · first reset {_hm(soon)}" if soon else ""))
    elif outside is not None:
        line += " │ " + c("33", f"⚠ opened outside cas, which is on {outside.name} → exit, then: {fix}")
    elif alt:
        left = "?" if alt.eff.five_pct is None else f"{100 - alt.eff.five_pct:.0f}%"
        line += c("2", f" │ next: {alt.profile.name} {left}")
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
    ours = render(cfg, p, now, keychain_locked=locked, outside=outside, fix=fix)
    chain = cfg.settings.get("statusline_chain")
    if chain:
        before = run_chain(chain, raw)
        if before:
            first, _, rest = before.partition("\n")
            return f"{first}  {ours}" + (f"\n{rest}" if rest else "")
    return ours
