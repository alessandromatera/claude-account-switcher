"""cas: run Claude Code on several accounts with one shared memory.

  cas init                     set up (status line, /accounts command, `cas` on PATH)
  cas add <name>               new profile + browser login for another account
  cas status                   what's left on every account: 5-hour, weekly, limit resets, credits
  cas run [name|auto] [args]   start Claude Code on an account; offers a handoff at the limit
  cas handoff [name]           resume the last conversation here on another account
  cas reset [name]             use a limit reset (opens Claude Code's /limit-reset; you confirm)
  cas ping <name|all>          open a 5-hour window now with a tiny message
  cas plan / cas schedule      plan and schedule window-opening pings (launchd)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, launchd, picker, planner, resets, shared
from . import profiles as prof
from .config import (
    NAME_RULES,
    Config,
    Profile,
    UnknownProfile,
    Unreadable,
    backups_dir,
    cas_home,
    home,
    hub_dir,
    profiles_root,
    read_json,
    read_json_strict,
    state_dir,
    valid_name,
    write_json_atomic,
    write_text_atomic,
)
from .usage import (
    FIVE_HOURS,
    Effective,
    Snapshot,
    Window,
    best_snapshot,
    effective,
    extra_summary,
    load_state,
    record_snapshot,
    refresh,
    save_state,
)

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "cas"
MARKER = "managed by claude-account-switcher"
LIMIT_RE = re.compile(
    r"hit your (?:session |weekly |opus |sonnet |fable )?limit|usage limit reached|limit reached|rate_limit_error",
    re.I,
)
PING_PROMPT = "Reply with exactly one word: ok"

# Flags carried over when a conversation moves to another account (others, like a prompt, are not).
CARRY_FLAGS_NOARG = {"--dangerously-skip-permissions", "--allow-dangerously-skip-permissions", "--chrome", "--verbose"}
CARRY_FLAGS_ARG = {"--model", "--permission-mode", "--agent", "--settings", "--mcp-config", "--effort"}
CARRY_FLAGS_MULTI = {"--add-dir"}


# --------------------------------------------------------------------------- output helpers

class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def c(self, code: str, s: str) -> str:
        return f"\033[{code}m{s}\033[0m" if self.on else s

    def dim(self, s: str) -> str:
        return self.c("2", s)

    def bold(self, s: str) -> str:
        return self.c("1", s)

    def warn(self, s: str) -> str:
        return self.c("33", s)

    def bad(self, s: str) -> str:
        return self.c("31", s)

    def good(self, s: str) -> str:
        return self.c("32", s)

    def left(self, left: Optional[float], s: str) -> str:
        if left is None:
            return self.dim(s)
        return self.c("32" if left > 40 else ("33" if left > 15 else "31"), s)


def make_style(plain: bool = False) -> Style:
    return Style(not plain and sys.stdout.isatty() and not os.environ.get("NO_COLOR"))


ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def vlen(s: str) -> int:
    return len(ANSI_RE.sub("", s))


def table(rows: List[List[str]], gap: int = 2) -> str:
    widths = [max(vlen(r[i]) for r in rows) for i in range(len(rows[0]))]
    out = []
    for r in rows:
        out.append((" " * gap).join(cell + " " * (widths[i] - vlen(cell)) for i, cell in enumerate(r)).rstrip())
    return "\n".join(out)


def when(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return "-"
    now = time.time() if now is None else now
    lt, ln = time.localtime(ts), time.localtime(now)
    if lt.tm_yday == ln.tm_yday and lt.tm_year == ln.tm_year:
        return time.strftime("%H:%M", lt)
    if ts - now < 6 * 86400:
        return time.strftime("%a %H:%M", lt)
    return time.strftime("%b %d", lt).replace(" 0", " ")


def rel(ts: Optional[float], now: Optional[float] = None) -> str:
    if not ts:
        return ""
    now = time.time() if now is None else now
    d = int(ts - now)
    if d <= 60:
        return "now"
    h, m = divmod(d // 60, 60)
    if h >= 48:
        return f"in {h // 24}d"
    return f"in {h}h{m:02d}m" if h else f"in {m}m"


def age(sec: Optional[float]) -> str:
    if sec is None:
        return "no data"
    sec = int(sec)
    if sec < 90:
        return f"{sec}s ago"
    if sec < 5400:
        return f"{sec // 60}m ago"
    if sec < 172800:
        return f"{sec // 3600}h ago"
    return f"{sec // 86400}d ago"


def die(msg: str, code: int = 1) -> int:
    print(f"cas: {msg}", file=sys.stderr)
    return code


def confirm(question: str, default: bool = False) -> bool:
    if not sys.stdin.isatty():
        return default
    hint = "[Y/n]" if default else "[y/N]"
    try:
        ans = input(f"{question} {hint} ").strip().lower()
    except EOFError:
        return default
    return default if not ans else ans in ("y", "yes", "s", "si", "sì")


# --------------------------------------------------------------------------- data gathering

@dataclass
class Row:
    profile: Profile
    snap: Optional[Snapshot]
    eff: Effective
    resets: Optional[resets.ResetStatus]
    error: Optional[str]


def gather(cfg: Config, live: Optional[bool] = None, now: Optional[float] = None) -> List[Row]:
    rows = []
    for p in cfg.profile_list():
        if p.logged_in() and p.enabled:
            snap, err = refresh(p, cfg.settings, live)
        else:
            snap, err = best_snapshot(p), None
        t = time.time() if now is None else now
        rows.append(Row(p, snap, effective(snap, t), resets.best_status(p, t), err))
    return rows


def in_claude_code() -> bool:
    return os.environ.get("CLAUDECODE") == "1"


def find_claude(cfg: Config) -> str:
    cand = cfg.settings.get("claude_bin")
    if cand and os.access(cand, os.X_OK):
        return cand
    found = shutil.which("claude")
    if found:
        return found
    fallback = home() / ".local" / "bin" / "claude"
    if os.access(fallback, os.X_OK):
        return str(fallback)
    raise SystemExit(die("claude not found on PATH; install Claude Code first"))


def cwd_candidates() -> List[str]:
    out = [os.getcwd()]
    pwd = os.environ.get("PWD")
    if pwd and pwd not in out:
        out.append(pwd)
    return out


def sanitize(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def latest_session(cfg: Config, cwds: Sequence[str], since: Optional[float] = None) -> Optional[str]:
    cands: List[Tuple[float, str]] = []
    for p in cfg.profile_list():
        last = load_state(p.name).get("last_session") or {}
        if last.get("id") and last.get("cwd") in cwds and (since is None or float(last.get("ts", 0)) >= since):
            cands.append((float(last.get("ts", 0)), last["id"]))
    for cwd in cwds:
        d = hub_dir() / "projects" / sanitize(cwd)
        if d.is_dir():
            for f in d.glob("*.jsonl"):
                try:
                    m = f.stat().st_mtime
                except OSError:
                    continue
                if since is None or m >= since:
                    cands.append((m, f.stem))
    return max(cands)[1] if cands else None


CLAUDE_SUBCOMMANDS = {"agents", "attach", "auth", "auto-mode", "doctor", "gateway", "import", "install", "logs",
                      "mcp", "plugin", "plugins", "purge", "respawn", "rm", "setup-token", "stop", "kill",
                      "ultrareview", "update", "upgrade"}


def session_from_args(args: Sequence[str]) -> Tuple[Optional[str], bool]:
    """(session id these claude args will use, whether they already pick a session)."""
    picks, sid = False, None
    for i, a in enumerate(args):
        key, _, val = a.partition("=")
        if key in ("-c", "--continue", "--fork-session"):
            picks, sid = True, None
            if key == "--fork-session":
                return None, True
        elif key in ("-r", "--resume", "--session-id"):
            picks = True
            nxt = val or (args[i + 1] if i + 1 < len(args) and not args[i + 1].startswith("-") else "")
            sid = nxt or None
    return sid, picks


def session_profile(cfg: Config, session: str) -> Optional[Profile]:
    """The account a conversation last ran on, from what the status line recorded."""
    best: Optional[Tuple[float, Profile]] = None
    for p in cfg.profile_list():
        st = load_state(p.name)
        seen = []
        if (st.get("last_session") or {}).get("id") == session:
            seen.append(float(st["last_session"].get("ts", 0)))
        recent = st.get("recent_sessions")
        if isinstance(recent, dict) and session in recent:
            seen.append(float(recent[session]))
        if seen and (best is None or max(seen) > best[0]):
            best = (max(seen), p)
    if best:
        return best[1]
    last = global_state().get("last_profile")
    return cfg.profiles.get(last) if last in cfg.profiles else None


def transcript_path(session: str) -> Optional[Path]:
    hits = list((hub_dir() / "projects").glob(f"*/{session}.jsonl"))
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def first_prompt(path: Path, limit: int = 60) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for _, line in zip(range(200), fh):
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(obj, dict) or obj.get("type") != "user":
                    continue
                content = (obj.get("message") or {}).get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
                text = " ".join(str(content or "").split())
                if text and not text.startswith("<"):
                    return text[:limit] + ("…" if len(text) > limit else "")
    except OSError:
        pass
    return "(no prompt)"


def sessions_here(cwds: Sequence[str], within_s: float = 86400) -> List[Tuple[float, str, Path]]:
    """(last activity, id, transcript) of this directory's conversations, newest first."""
    now, seen, out = time.time(), set(), []
    for cwd in cwds:
        d = hub_dir() / "projects" / sanitize(cwd)
        if not d.is_dir():
            continue
        for f in d.glob("*.jsonl"):
            try:
                m = f.stat().st_mtime
            except OSError:
                continue
            if now - m <= within_s and f.stem not in seen:
                seen.add(f.stem)
                out.append((m, f.stem, f))
    return sorted(out, reverse=True)


def choose_session(cfg: Config, cwds: Sequence[str], style: Style) -> Optional[str]:
    """The conversation to hand off: ask when several in this directory were used recently."""
    recent = sessions_here(cwds)
    if not recent:
        return latest_session(cfg, cwds)
    now = time.time()
    busy = [r for r in recent if now - r[0] < 6 * 3600]
    if len(busy) <= 1 or not sys.stdin.isatty():
        if len(busy) > 1:
            print(style.dim(f"{len(busy)} recent conversations here; taking the latest ({recent[0][1][:8]}). "
                            "Pick another with --session <id>."))
        return recent[0][1]
    print("Conversations in this directory:")
    for i, (m, sid, path) in enumerate(busy, 1):
        live = style.warn(" · open right now?") if now - m < 90 else ""
        print(f"  [{i}] {sid[:8]}  {age(now - m):>8}  {first_prompt(path)}{live}")
    try:
        ans = input("Which one? (Enter = 1): ").strip()
    except EOFError:
        ans = ""
    try:
        idx = int(ans) - 1 if ans else 0
    except ValueError:
        return None
    return busy[idx][1] if 0 <= idx < len(busy) else None


def transcript_says_limit(session: Optional[str], cwds: Sequence[str], since: float) -> bool:
    if not session:
        return False
    found = transcript_path(session)
    for f in ([found] if found else [hub_dir() / "projects" / sanitize(c) / f"{session}.jsonl" for c in cwds]):
        if not f.exists():
            continue
        try:
            with open(f, "rb") as fh:
                fh.seek(max(0, f.stat().st_size - 256 * 1024))
                tail = fh.read().decode("utf-8", "replace").splitlines()[-80:]
        except OSError:
            return False
        from .usage import parse_time

        for line in tail:
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            if not isinstance(obj, dict):
                continue
            ts = parse_time(obj.get("timestamp"))
            if ts is not None and ts < since:
                continue
            if obj.get("isApiErrorMessage") or obj.get("type") == "system" or obj.get("error"):
                if LIMIT_RE.search(json.dumps(obj)):
                    return True
    return False


def carry_flags(args: Sequence[str]) -> List[str]:
    out: List[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        key = a.split("=", 1)[0]
        if key in CARRY_FLAGS_NOARG:
            out.append(a)
        elif key in CARRY_FLAGS_ARG:
            if "=" in a:
                out.append(a)
            elif i + 1 < len(args):
                out += [a, args[i + 1]]
                i += 1
        elif key in CARRY_FLAGS_MULTI:
            out.append(a)
            while i + 1 < len(args) and not args[i + 1].startswith("-"):
                out.append(args[i + 1])
                i += 1
        i += 1
    return out


def has_flag(args: Sequence[str], flag: str) -> bool:
    return any(a == flag or a.startswith(flag + "=") for a in args)


def global_state() -> Dict[str, Any]:
    return load_state("_global")


def set_global(**kv: Any) -> None:
    st = global_state()
    st.update(kv)
    save_state("_global", st)


# --------------------------------------------------------------------------- what's left

def left_line(r: Row, style: Style, now: float) -> str:
    e = r.eff
    if not r.profile.logged_in():
        return style.dim("not logged in")
    bits = []
    if e.five_pct is None:
        bits.append(style.dim("5h: ?"))
    else:
        left = max(0.0, 100 - e.five_pct)
        t = f"5h: {left:.0f}% left"
        if e.five_resets:
            t += f" (resets {when(e.five_resets, now)}, {rel(e.five_resets, now)})"
        elif e.window == "idle":
            t += " (no window open)"
        bits.append(style.left(left, t))
    if e.week_pct is not None:
        wl = max(0.0, 100 - e.week_pct)
        bits.append(style.left(wl, f"week: {wl:.0f}% left") + (style.dim(f" (resets {when(e.week_resets, now)})") if e.week_resets else ""))
    if r.resets:
        bits.append("limit resets: " + r.resets.summary(now))
    if r.snap and r.snap.extra:
        bits.append("credits: " + extra_summary(r.snap.extra))
    return " · ".join(bits)


def print_status(cfg: Config, rows: List[Row], style: Style, now: float, mark_current: bool) -> None:
    cur = cfg.current() if mark_current else None
    head = ["", "PROFILE", "ACCOUNT", "PLAN", "5-HOUR LEFT", "WEEKLY LEFT", "LIMIT RESETS", "CREDITS", "DATA"]
    out = [[style.dim(h) for h in head]]
    for r in rows:
        p, e = r.profile, r.eff
        if not p.logged_in():
            five = week = style.dim("-")
        else:
            if e.five_pct is None:
                five = style.dim("?")
            else:
                left = max(0.0, 100 - e.five_pct)
                tail = f" until {when(e.five_resets, now)}" if e.five_resets else (" idle" if e.window == "idle" else "")
                five = style.left(left, f"{left:.0f}%") + style.dim(tail)
            if e.week_pct is None:
                week = style.dim("?")
            else:
                wl = max(0.0, 100 - e.week_pct)
                week = style.left(wl, f"{wl:.0f}%") + style.dim(f" until {when(e.week_resets, now)}" if e.week_resets else "")
        rs = r.resets.summary(now, short=True) if r.resets else style.dim("?")
        if r.resets and r.resets.has_any(now):
            rs = style.c("36", rs)
        credits = extra_summary(r.snap.extra).split(" (")[0] if r.snap and r.snap.extra else style.dim("?")
        data = style.dim(f"{e.source} {age(e.age)}" if e.source else "no data")
        if r.error and p.logged_in():
            data += style.dim(" · live: " + r.error.split(" (")[0])
        name = p.name + ("" if p.enabled else style.dim(" (off)"))
        out.append(["*" if cur and cur.name == p.name else " ", style.bold(name),
                    p.email or style.dim("not logged in"), p.plan, five, week, rs, credits, data])
    print(table(out))

    ordered, blocked = picker.rank([(r.profile, r.eff) for r in rows], cfg.settings)
    print()
    if ordered:
        c = ordered[0]
        print(f"Next up: {style.bold(c.profile.name)} · {c.reason}")
    else:
        print(style.warn("No account has room right now."))
    for b in blocked:
        if b.reason == "not logged in":
            print(style.dim(f"  {b.profile.name}: not logged in → cas login {b.profile.name}"))
            continue
        msg = f"  {b.profile.name}: {b.reason}" + (f", resets {when(b.until, now)} ({rel(b.until, now)})" if b.until else "")
        row = next(r for r in rows if r.profile.name == b.profile.name)
        if row.resets and row.resets.has_any(now):
            msg += style.c("36", f" · limit reset available → cas reset {b.profile.name}")
        elif row.resets and row.resets.maybe(now):
            msg += style.dim(f" · may have a limit reset → cas reset {b.profile.name}")
        print(msg)
    if any(r.resets and r.resets.has_any(now) for r in rows):
        print(style.dim("Limit resets are only used when you ask: `cas reset <profile>` opens Claude Code's "
                        "/limit-reset and you confirm there."))


# --------------------------------------------------------------------------- decisions at the limit

@dataclass
class Action:
    kind: str  # switch | reset | wait | start
    profile: Optional[Profile] = None
    grant: Optional[resets.Grant] = None


def _needed_window(e: Effective, settings: Dict[str, Any]) -> str:
    if e.week_pct is not None and e.week_pct >= float(settings.get("weekly_threshold", 97)):
        return "seven_day"
    return "five_hour"


def decide(cfg: Config, rows: List[Row], style: Style, current: Optional[Profile], mode: str,
           now: Optional[float] = None, also_exclude: Sequence[str] = (),
           no_reset_for: Sequence[str] = ()) -> Action:
    """At a limit: show what's left everywhere, then switch account, use a limit reset, or wait."""
    now = time.time() if now is None else now
    s = cfg.settings
    exclude = ({current.name} if current else set()) | set(also_exclude)
    ordered, blocked = picker.rank([(r.profile, r.eff) for r in rows], s, exclude)
    alt = ordered[0] if ordered else None

    reset_rows = [r for r in rows if (r.profile.name == current.name if current else
                                      any(b.profile.name == r.profile.name for b in blocked))]
    reset_opt: Optional[Tuple[Row, Optional[resets.Grant]]] = None
    reset_unsure = False  # cas can't see the resets; /limit-reset in Claude Code will show them
    for r in reset_rows:
        if not r.resets or r.profile.name in no_reset_for:
            continue
        need = _needed_window(r.eff, s)
        grants = [g for g in r.resets.usable(now) if not g.clears or need in g.clears]
        if grants:
            reset_opt = (r, grants[0])
            break
        if r.resets.count(now) is None and r.resets.has_any(now):
            reset_opt = (r, None)
            break
        if r.resets.maybe(now) and reset_opt is None:
            reset_opt, reset_unsure = (r, None), True

    if current:
        crow = next(r for r in rows if r.profile.name == current.name)
        wait_until = crow.eff.week_resets if _needed_window(crow.eff, s) == "seven_day" else crow.eff.five_resets
        what = "weekly" if _needed_window(crow.eff, s) == "seven_day" else "5-hour"
        print()
        print(style.warn(f"⚠ {current.name} reached its {what} limit") +
              (f" · resets {when(wait_until, now)} ({rel(wait_until, now)})" if wait_until else ""))
    else:
        soon = picker.soonest_unblock(blocked)
        wait_until = soon.until if soon else None
        print(style.warn("⚠ Every account is at its limit") +
              (f" · first one frees up {when(wait_until, now)} ({rel(wait_until, now)})" if wait_until else ""))

    print("What's left:")
    w = max(len(r.profile.name) for r in rows)
    for r in rows:
        print(f"  {r.profile.name.ljust(w)}  {left_line(r, style, now)}")

    options: List[Tuple[Action, str]] = []
    if alt:
        verb = "continue this conversation on" if current else "start on"
        options.append((Action("switch", alt.profile), f"{verb} {alt.profile.name} ({alt.reason})"))
    if reset_opt and reset_unsure:
        r, _ = reset_opt
        options.append((Action("reset", r.profile, None),
                        f"check for a limit reset on {r.profile.name}: opens /limit-reset in Claude Code, which "
                        "shows what you have and asks before using one"))
    elif reset_opt:
        r, g = reset_opt
        desc = g.name if g else "a limit reset"
        extra = f", {g.left} left" + (f", use by {when(g.ends_at, now)}" if g.ends_at else "") if g else ""
        options.append((Action("reset", r.profile, g),
                        f"use {desc} on {r.profile.name}{extra}: opens Claude Code's /limit-reset, you confirm there"))
    if current:
        options.append((Action("wait"), "stop here and wait" + (f" for {when(wait_until, now)}" if wait_until else "")))
    else:
        if blocked and blocked[0].profile.logged_in():
            options.append((Action("start", blocked[0].profile), f"start on {blocked[0].profile.name} anyway"))
        options.append((Action("wait"), "do nothing"))

    rec = 0
    if not alt and reset_opt and reset_unsure:
        rec = len(options) - 1  # don't recommend something cas can't confirm exists
    elif not alt and reset_opt:
        g = reset_opt[1]
        long_wait = wait_until is None or wait_until - now > 45 * 60
        expiring = g is not None and g.ends_at is not None and wait_until is not None and g.ends_at < wait_until + 3600
        rec = 0 if (long_wait or expiring) else len(options) - 1
    if not alt and not reset_opt:
        rec = len(options) - 1

    if mode == "auto" and alt:
        print(style.dim(f"handoff: auto → {alt.profile.name}"))
        return options[0][0]
    if not sys.stdin.isatty():
        print("Options:")
        for a, label in options:
            print(f"  - {label}")
        return Action("wait")
    print()
    for i, (_, label) in enumerate(options, 1):
        tag = style.good(" (recommended)") if i - 1 == rec else ""
        print(f"  [{i}] {label}{tag}")
    try:
        ans = input(f"Choose 1-{len(options)} (Enter = {rec + 1}): ").strip()
    except EOFError:
        ans = ""
    try:
        idx = int(ans) - 1 if ans else rec
        return options[idx][0] if 0 <= idx < len(options) else Action("wait")
    except ValueError:
        return Action("wait")


# --------------------------------------------------------------------------- running claude

def resolve_target(cfg: Config, target: str, style: Style) -> Tuple[Optional[Profile], List[str]]:
    """Returns (profile, extra claude args). Extra args are set when the choice is a limit reset."""
    if target != "auto":
        p = cfg.get(target)
        if not p.logged_in():
            print(style.warn(f"{p.name} is not logged in; Claude Code will ask you to log in."))
        return p, []
    rows = gather(cfg)
    ordered, _ = picker.rank([(r.profile, r.eff) for r in rows], cfg.settings)
    if ordered:
        c = ordered[0]
        print(style.dim(f"auto → {c.profile.name} · {c.reason}"))
        return c.profile, []
    a = decide(cfg, rows, style, None, "ask")
    if a.kind in ("switch", "start"):
        return a.profile, []
    if a.kind == "reset":
        return a.profile, resets.limit_reset_arg(a.grant)
    return None, []


def prime_others(cfg: Config, target: Profile, style: Style, now: Optional[float] = None) -> List[str]:
    """Background-ping every other account with no open window, so its clock starts now.

    It can't hurt: the ping is one tiny message and only goes to accounts whose window is closed,
    and an earlier window start means an earlier reset if you switch to that account later.
    """
    if not cfg.settings.get("prime_others_on_start", True):
        return []
    now = time.time() if now is None else now
    started = []
    for q in cfg.profile_list():
        if q.name == target.name or not q.enabled or not q.logged_in():
            continue
        e = effective(best_snapshot(q), now)
        if e.window == "active" or picker.exhausted(e, cfg.settings):
            continue
        last = load_state(q.name).get("prime_spawned_at", 0)
        if now - float(last) < 600:  # one already on its way
            continue
        log = cas_home() / "logs" / f"prime-{q.name}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            with open(log, "a") as fh:
                subprocess.Popen([sys.executable, str(SCRIPT), "ping", q.name], stdin=subprocess.DEVNULL,
                                 stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())
        except OSError as ex:
            print(style.dim(f"· could not open {q.name}'s window: {ex}"))
            continue
        st = load_state(q.name)
        st["prime_spawned_at"] = now
        save_state(q.name, st)
        started.append(q.name)
    if started:
        print(style.dim(f"· opening the 5-hour window of {', '.join(started)} in the background "
                        "(its reset comes sooner if you switch; prime_others_on_start)"))
    return started


def run_claude(claude: str, args: List[str], p: Profile) -> int:
    try:
        return subprocess.call([claude, *args], env=p.env())
    except KeyboardInterrupt:
        return 130


def run_loop(cfg: Config, target: str, claude_args: List[str], mode: str, style: Style) -> int:
    if in_claude_code():
        return die("you are inside a Claude Code session; exit it first, then run this from your terminal")
    claude = find_claude(cfg)
    cwds = cwd_candidates()
    rc = 0
    pending: List[str] = []  # one-shot trailing prompt, e.g. "/limit-reset"
    limited: List[str] = []  # accounts that hit their limit during this run: never hand back to them
    reset_offered: List[str] = []  # offer a limit reset at most once per account per run
    while True:
        p, extra = resolve_target(cfg, target, style)
        if p is None:
            return rc
        for a in prof.prepare(p, cfg.profile_list(), cfg.settings.get("sync_mcp", True)):
            print(style.dim(f"· {a}"))
        args = list(claude_args)
        if p.model and not has_flag(args, "--model"):
            args = ["--model", p.model] + args
        args += extra + pending
        pending = []
        # Name the conversation ourselves, so the handoff resumes exactly this one even when
        # other Claude Code sessions are open in the same directory.
        own, picks = session_from_args(args)
        if not picks and not (args and args[0] in CLAUDE_SUBCOMMANDS):
            own = str(uuid.uuid4())
            args = ["--session-id", own] + args
        started = time.time()
        set_global(last_profile=p.name)
        prime_others(cfg, p, style, started)
        print(style.dim(f"→ Claude Code on {p.name} ({p.display()})"))
        rc = run_claude(claude, args, p)
        try:
            for a in prof.sync_files(p):
                print(style.dim(f"· {a}"))
        except OSError as e:
            print(style.dim(f"· sync failed: {e}"))
        if mode == "off":
            return rc
        session = own if own and transcript_path(own) else latest_session(cfg, cwds, since=started - 5)
        snap, _ = refresh(p, cfg.settings)
        e = effective(snap)
        if not (picker.exhausted(e, cfg.settings) or transcript_says_limit(session, cwds, started)):
            return rc
        if p.name not in limited:
            limited.append(p.name)
        rows = gather(cfg)
        action = decide(cfg, rows, style, p, mode, also_exclude=limited, no_reset_for=reset_offered)
        resume = ["--resume", session] if session else ["--continue"]
        if action.kind == "switch" and action.profile:
            target, claude_args = action.profile.name, carry_flags(claude_args) + resume
            continue
        if action.kind == "reset" and action.profile:
            target = action.profile.name
            reset_offered.append(target)
            if target in limited:
                limited.remove(target)  # it may have room again after the reset
            claude_args = carry_flags(claude_args) + (["--resume", session] if session else [])
            pending = resets.limit_reset_arg(action.grant)
            print(style.dim("Opening /limit-reset in Claude Code. Nothing is used until you confirm there "
                            "(if no dialog appears, type /limit-reset)."))
            continue
        return rc


def split_run_args(argv: List[str], cfg: Config) -> Tuple[Dict[str, Any], str, List[str]]:
    opts: Dict[str, Any] = {"mode": None}
    i = 0
    while i < len(argv) and argv[i] in ("-y", "--yes", "--no-handoff", "--ask", "-h", "--help"):
        a = argv[i]
        if a in ("-h", "--help"):
            opts["help"] = True
        elif a in ("-y", "--yes"):
            opts["mode"] = "auto"
        elif a == "--no-handoff":
            opts["mode"] = "off"
        else:
            opts["mode"] = "ask"
        i += 1
    target = "auto"
    if i < len(argv) and not argv[i].startswith("-") and (argv[i] in cfg.profiles or argv[i] == "auto"):
        target = argv[i]
        i += 1
    if i < len(argv) and argv[i] == "--":
        i += 1
    return opts, target, argv[i:]


RUN_HELP = """usage: cas run [-y|--no-handoff|--ask] [PROFILE|auto] [--] [claude args...]

Start Claude Code on PROFILE (default: auto = the account whose quota expires soonest and
still has room). Everything after the profile goes to claude unchanged.

When Claude exits because the account hit its limit, cas shows what's left on every account
and offers to continue the same conversation on another one, to use a limit reset (through
Claude Code's own /limit-reset, which asks you to confirm), or to wait.

  -y, --yes       hand off to the next account without asking (never uses a limit reset)
  --no-handoff    just run claude
  --ask           ask before handing off (default; see `handoff` in config)
"""


def cmd_run(argv: List[str]) -> int:
    cfg = Config.load()
    opts, target, rest = split_run_args(argv, cfg)
    if opts.get("help"):
        print(RUN_HELP)
        return 0
    mode = opts["mode"] or cfg.settings.get("handoff", "ask")
    try:
        return run_loop(cfg, target, rest, mode, make_style())
    except UnknownProfile as e:
        return die(str(e))


def cmd_handoff(args: argparse.Namespace) -> int:
    cfg = Config.load()
    style = make_style()
    session = args.session or choose_session(cfg, cwd_candidates(), style)
    if not session:
        return die("no conversation found for this directory; run `cas run` instead")
    t = transcript_path(session)
    if t and time.time() - t.stat().st_mtime < 90 and \
            not confirm(f"{session[:8]} was active less than 90s ago; if it's still open elsewhere, both would "
                        "write to it. Continue?", default=False):
        return 1
    src = session_profile(cfg, session)
    target = args.profile
    if not target:
        rows = gather(cfg)
        c = picker.best([(r.profile, r.eff) for r in rows], cfg.settings, exclude={src.name} if src else ())
        src_row = next((r for r in rows if src and r.profile.name == src.name), None)
        if c:
            target = c.profile.name
            print(style.dim(f"handoff {src.name if src else '?'} → {target} · {c.reason}"))
        elif src_row and not picker.exhausted(src_row.eff, cfg.settings):
            left = "?" if src_row.eff.five_pct is None else f"{100 - src_row.eff.five_pct:.0f}%"
            print(f"No other account has room right now; {src.name}, where this conversation ran, still has "
                  f"{left} of its 5-hour window.")
            if not confirm(f"Resume it on {src.name}?", default=True):
                return 0
            target = src.name
        else:
            a = decide(cfg, rows, style, src, "ask")
            if a.kind == "reset" and a.profile:
                return run_loop(cfg, a.profile.name, ["--resume", session] + resets.limit_reset_arg(a.grant),
                                "ask", style)
            if a.kind not in ("switch", "start") or not a.profile:
                return 0
            target = a.profile.name
    mode = "auto" if args.yes else cfg.settings.get("handoff", "ask")
    return run_loop(cfg, target, ["--resume", session] + list(args.claude_args or []), mode, style)


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = Config.load()
    style = make_style()
    now = time.time()
    if args.profile:
        p = cfg.get(args.profile)
    else:
        last = global_state().get("last_profile")
        p = cfg.get(last) if last in cfg.profiles else cfg.hub()
    snap, err = refresh(p, cfg.settings, live=True)
    row = Row(p, snap, effective(snap, now), resets.best_status(p, now), err)
    print(f"{style.bold(p.name)} ({p.display()})")
    print("  " + left_line(row, style, now))
    rs = row.resets
    if err:
        print(style.dim(f"  live data unavailable: {err}"))
    if not rs or not (rs.has_any(now) or rs.maybe(now)):
        print("No limit reset on this account: " + (rs.summary(now) if rs else "cas has no information") + ".")
        return 1
    if rs.maybe(now):
        print("  cas can't see this account's limit resets: the server shares them only with Claude Code itself. "
              "/limit-reset shows what you have.")
    grants = rs.usable(now)
    for g in grants:
        notes = []
        if g.clears:
            notes.append("clears " + ", ".join(resets.WINDOW_NAMES.get(c, c) for c in g.clears))
        if g.ends_at:
            notes.append(f"use by {when(g.ends_at, now)}")
        if g.requires_limit and not g.usable_now:
            notes.append("only usable once you hit the limit")
        if g.unavailable_reason:
            notes.append(f"unavailable now: {g.unavailable_reason.replace('_', ' ')}")
        print(f"  ⟲ {g.left}× {g.name}" + (style.dim(" · " + " · ".join(notes)) if notes else ""))
    if row.eff.five_pct is not None and row.eff.five_pct < float(cfg.settings.get("switch_threshold", 90)):
        print(style.warn(f"  This account still has {100 - row.eff.five_pct:.0f}% of its 5-hour window; "
                         "a reset is usually worth keeping for when you hit the limit."))
    others = picker.best([(r.profile, r.eff) for r in gather(cfg)], cfg.settings, exclude={p.name})
    if others:
        print(style.dim(f"  Alternative that costs nothing: {others.profile.name} ({others.reason})."))
    if not confirm("Open Claude Code's /limit-reset on this account? (you confirm the reset there)"):
        print("Nothing used.")
        return 0
    grant = next((g for g in grants if g.kind == args.kind), grants[0] if grants else None) if args.kind else None
    claude_args = (["--continue"] if args.cont else []) + resets.limit_reset_arg(grant)
    return run_loop(cfg, p.name, claude_args, cfg.settings.get("handoff", "ask"), style)


# --------------------------------------------------------------------------- simple commands

def cmd_status(args: argparse.Namespace) -> int:
    cfg = Config.load()
    live = True if args.live else (False if args.no_live else None)
    now = time.time()
    rows = gather(cfg, live, now)
    if args.json:
        out = []
        for r in rows:
            out.append({
                "profile": r.profile.name,
                "email": r.profile.email,
                "plan": r.profile.plan,
                "logged_in": r.profile.logged_in(),
                "five_hour": {"used_pct": r.eff.five_pct, "left_pct": r.eff.headroom, "resets_at": r.eff.five_resets,
                              "window": r.eff.window},
                "seven_day": {"used_pct": r.eff.week_pct, "resets_at": r.eff.week_resets},
                "limit_resets": r.resets.to_dict() if r.resets else None,
                "limit_resets_left": r.resets.count(now) if r.resets else None,
                "extra_usage": r.snap.extra if r.snap else None,
                "source": r.eff.source,
                "age_s": r.eff.age,
                "error": r.error,
            })
        c = picker.best([(r.profile, r.eff) for r in rows], cfg.settings)
        print(json.dumps({"profiles": out, "next": c.profile.name if c else None}, indent=2))
        return 0
    style = make_style(args.plain)
    print_status(cfg, rows, style, now, mark_current=in_claude_code() or bool(os.environ.get("CAS_PROFILE")))
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    cfg = Config.load()
    rows = gather(cfg)
    c = picker.best([(r.profile, r.eff) for r in rows], cfg.settings, exclude=args.exclude or ())
    if not c:
        return 1
    print(c.profile.name)
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    cfg = Config.load()
    p = cfg.get(args.profile)
    if p.config_dir is None:
        print("unset CLAUDE_CONFIG_DIR")
    else:
        print(f"export CLAUDE_CONFIG_DIR={shlex.quote(str(p.config_dir))}")
    print(f"export CAS_PROFILE={shlex.quote(p.name)}")
    return 0


def _login(cfg: Config, p: Profile, email: Optional[str]) -> int:
    claude = find_claude(cfg)
    cmd = [claude, "auth", "login", "--claudeai"] + (["--email", email] if email else [])
    print(f"Logging in profile '{p.name}'. A browser window opens: sign in with the account for this profile.")
    print("Tip: if the browser is already signed in to another Claude account, open the printed URL in a "
          "private window.")
    rc = subprocess.call(cmd, env=p.env())
    acct = p.oauth_account()
    if not acct.get("accountUuid"):
        print("Login did not complete. Retry with: cas login " + p.name)
        return rc or 1
    print(f"✓ {p.name}: {acct.get('emailAddress')} ({p.plan})")
    dup = [q.name for q in cfg.profile_list() if q.name != p.name and q.account_uuid == acct.get("accountUuid")]
    if dup:
        print(f"⚠ This is the same account as profile(s): {', '.join(dup)}. Sharing quota gains nothing; "
              f"log in with a different account: cas login {p.name}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    cfg = Config.load()
    name = args.name.lower()
    if not valid_name(name):
        return die(f"profile names: {NAME_RULES}")
    if name in cfg.profiles:
        return die(f"profile '{name}' already exists (re-login with: cas login {name})")
    config_dir = Path(os.path.abspath(profiles_root() / name))
    p = Profile(name=name, config_dir=config_dir, label=args.label, model=args.model)
    for a in prof.create_dir(p):
        print(f"· {a}")
    cfg.put(p)
    cfg.save()
    print(f"Profile '{name}' created at {config_dir}")
    if args.no_login:
        print(f"Log in later with: cas login {name}")
        return 0
    return _login(cfg, p, args.email)


def cmd_login(args: argparse.Namespace) -> int:
    cfg = Config.load()
    p = cfg.get(args.name)
    if not p.is_hub:
        prof.create_dir(p)
    return _login(cfg, p, args.email)


def cmd_remove(args: argparse.Namespace) -> int:
    cfg = Config.load()
    p = cfg.get(args.name)
    if p.is_hub:
        return die("the hub profile (~/.claude) can't be removed")
    if not args.yes and not confirm(f"Remove profile '{p.name}' ({p.display()})?"):
        return 1
    launchd.remove(p.name)
    if args.purge:
        try:
            subprocess.call([find_claude(cfg), "auth", "logout"], env=p.env())
        except SystemExit:
            pass
        d = p.config_dir
        if d and d.exists() and profiles_root() in d.parents:
            for item in d.iterdir():  # unlink shared-dir symlinks first so nothing in the hub is touched
                if item.is_symlink():
                    item.unlink()
            shutil.rmtree(d)
        for f in (state_dir() / f"{p.name}.json",):
            if f.exists():
                f.unlink()
        shutil.rmtree(cas_home() / "sync-base" / p.name, ignore_errors=True)
    cfg.remove(p.name)
    cfg.save()
    print(f"Removed '{p.name}'" + (" and its files" if args.purge else f" (files kept in {p.config_dir})"))
    return 0


def cmd_set(args: argparse.Namespace) -> int:
    cfg = Config.load()
    p = cfg.get(args.name)
    if args.label is not None:
        p.label = args.label or None
    if args.model is not None:
        p.model = args.model or None
    if args.enable:
        p.enabled = True
    if args.disable:
        p.enabled = False
    cfg.put(p)
    cfg.save()
    if args.rename_hub:
        if not p.is_hub:
            return die("--rename-hub only applies to the hub profile; use: cas rename OLD NEW")
        return rename_profile(cfg, p.name, args.rename_hub.lower())
    print("ok")
    return 0


def rename_profile(cfg: Config, old: str, new: str) -> int:
    """Rename a profile. Its config dir stays where it is, so the login (keyed by that path) survives."""
    p = cfg.get(old)
    if new == old:
        print("nothing to do")
        return 0
    if not valid_name(new):
        return die(f"profile names: {NAME_RULES}")
    if new in cfg.profiles:
        return die(f"'{new}' is already a profile")
    moved = []
    if launchd.supported() and launchd.rename(old, new):
        moved.append("schedule")
    cfg.rename(old, new)
    cfg.save()
    for src, dst in ((state_dir() / f"{old}.json", state_dir() / f"{new}.json"),
                     (cas_home() / "sync-base" / old, cas_home() / "sync-base" / new)):
        if src.exists() and not dst.exists():
            src.rename(dst)
    if global_state().get("last_profile") == old:
        set_global(last_profile=new)
    where = "~/.claude" if p.is_hub else str(p.config_dir)
    print(f"Renamed '{old}' → '{new}' ({p.email or 'not logged in'}; files stay in {where})"
          + (f"; moved its {', '.join(moved)}" if moved else ""))
    return 0


def cmd_rename(args: argparse.Namespace) -> int:
    return rename_profile(Config.load(), args.old, args.new.lower())


def cmd_sync(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.hook:
        return hook_sync(cfg, args.quick)
    targets = cfg.profile_list() if not args.profile else [cfg.get(args.profile)]
    n = 0
    for p in targets:
        if p.is_hub or not p.dir.exists():
            continue
        acts = prof.ensure_links(p) + prof.sync_files(p)
        m = prof.sync_mcp(p) if cfg.settings.get("sync_mcp", True) else None
        if m:
            acts.append(m)
        for a in acts:
            print(f"{p.name}: {a}")
        n += len(acts)
    if cfg.settings.get("shared_store"):
        acts = shared.sync(cfg.settings, quick=args.quick)
        _log_share(acts)
        for a in acts:
            print(f"machines: {a}")
        n += len(acts)
    if not n:
        print("everything in sync")
    return 0


def _log_share(actions: List[str]) -> None:
    if not actions:
        return
    log = cas_home() / "logs" / "share.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(log, "a", encoding="utf-8") as fh:
        for a in actions:
            fh.write(f"{stamp} {a}\n")


def hook_sync(cfg: Config, quick: bool) -> int:
    """Claude Code hook entry point. Prints nothing (SessionStart output would reach the model)."""
    try:
        data = json.loads(sys.stdin.read() or "{}")
    except (ValueError, OSError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not cfg.settings.get("shared_store"):
        return 0
    if data.get("cwd") and Path(data["cwd"]).resolve() == (cas_home() / "ping-workdir").resolve():
        return 0  # a priming ping: nothing to share
    key = Path(data["transcript_path"]).parent.name if quick and data.get("transcript_path") else None
    try:
        _log_share(shared.sync(cfg.settings, quick=quick, only_key=key, wait=False))
    except Exception as e:  # a failing hook must never get in the way of Claude Code
        _log_share([f"sync failed: {e.__class__.__name__}: {e}"])
    return 0


SHARE_HOOKS = (("SessionStart", False), ("Stop", True))


def _is_share_hook(entry: Any) -> bool:
    return isinstance(entry, dict) and any(
        isinstance(h, dict) and "bin/cas" in str(h.get("command", "")) and "sync --hook" in str(h.get("command", ""))
        for h in entry.get("hooks") or [])


def set_share_hooks(enable: bool) -> str:
    path = hub_dir() / "settings.json"
    try:
        data = read_json_strict(path) or {}
    except Unreadable:
        return f"skipped hooks: {path} is not valid JSON"
    if not isinstance(data, dict):
        return f"skipped hooks: {path} is not a JSON object"
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    before = json.dumps(hooks, sort_keys=True)
    for event, quick in SHARE_HOOKS:
        entries = [e for e in (hooks.get(event) or []) if not _is_share_hook(e)]
        if enable:
            cmd = f"{cas_command()} sync --hook" + (" --quick" if quick else "")
            entries.append({"hooks": [{"type": "command", "command": cmd, "timeout": 60}]})
        if entries:
            hooks[event] = entries
        else:
            hooks.pop(event, None)
    if json.dumps(hooks, sort_keys=True) == before:
        return "sync hooks already " + ("installed" if enable else "absent")
    if hooks:
        data["hooks"] = hooks
    else:
        data.pop("hooks", None)
    if path.exists():
        backups_dir().mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups_dir() / f"settings.json.before-share-hooks.{time.strftime('%Y%m%d-%H%M%S')}")
    write_json_atomic(path, data)
    return "sync hooks " + ("installed (session start: everything; after each reply: that project's memory)"
                            if enable else "removed")


def share_hooks_installed() -> bool:
    data = read_json(hub_dir() / "settings.json", {}) or {}
    hooks = data.get("hooks") if isinstance(data, dict) else None
    return isinstance(hooks, dict) and all(any(_is_share_hook(e) for e in hooks.get(ev) or []) for ev, _ in SHARE_HOOKS)


def cmd_share(args: argparse.Namespace) -> int:
    cfg = Config.load()
    s = cfg.settings
    if args.off:
        print("· " + set_share_hooks(False))
        s["shared_store"] = None
        cfg.save()
        print("Sharing between machines is off. The shared folder and local files are untouched.")
        return 0
    if args.status:
        store = shared.store_path(s)
        if not store:
            print("Not sharing memory between machines. Turn it on with: cas share --store <a folder your machines keep in sync>")
            return 0
        print(f"Shared folder: {store}")
        print(f"Projects root: {shared.projects_root(s)}")
        print(f"Hooks:         {'installed' if share_hooks_installed() else 'missing (cas share)'}")
        for m in shared.machines(store):
            print(f"  {m.get('host')}: {m.get('home')} · last sync {age(time.time() - float(m.get('synced_at', 0)))}")
        n = len(list((store / 'memory').iterdir())) if (store / "memory").is_dir() else 0
        print(f"Projects with shared memory: {n}")
        return 0

    store_arg = args.store or s.get("shared_store")
    if not store_arg:
        return die("say where: cas share --store <a folder your machines keep in sync>")
    if args.projects_root:
        s["projects_root"] = args.projects_root
    root = shared.projects_root(s)
    store = Path(os.path.expanduser(store_arg))
    if not root.is_dir():
        return die(f"{root} doesn't exist; pass --projects-root")
    if not store.parent.is_dir():
        return die(f"{store.parent} doesn't exist (is the shared folder synced to this machine?)")
    first = not shared.base_root().exists()
    if first:
        print(f"· backup of this machine's memory: {shared.backup(s)}")
    s["shared_store"] = store_arg
    cfg.save()
    acts = shared.sync(s)
    _log_share(acts)
    if not args.no_hooks:
        print("· " + set_share_hooks(True))
    print(f"· shared folder: {store}")
    for a in acts[:20]:
        print(f"  {a}")
    if len(acts) > 20:
        print(f"  … {len(acts) - 20} more (all in {cas_home() / 'logs' / 'share.log'})")
    others = [m for m in shared.machines(store) if m.get("host") != shared.host()]
    print(f"Done: {len(acts)} change(s)." + ("" if others else " Run `cas share --store <same folder>` on the other "
                                                               "machine once the folder has synced there."))
    return 0


def cmd_ping(args: argparse.Namespace) -> int:
    cfg = Config.load()
    names = [p.name for p in cfg.profile_list() if p.enabled and p.logged_in()] if args.profile == "all" else [args.profile]
    rc = 0
    for n in names:
        ok = do_ping(cfg, cfg.get(n), args.model, args.force, args.scheduled)
        rc = rc or (0 if ok else 1)
    return rc


def _log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


def schedule_settings(cfg: Config) -> Dict[str, Any]:
    return dict(cfg.settings.get("schedule") or {})


def scheduled_profiles(cfg: Config, sch: Dict[str, Any]) -> List[str]:
    names = sch.get("profiles")
    if names:
        return [n for n in names if n in cfg.profiles]
    return [p.name for p in cfg.profile_list() if p.enabled and p.logged_in()]


def build_plan(cfg: Config, sch: Dict[str, Any]) -> Tuple[Dict[str, List[int]], int, int, List[int]]:
    start, end = planner.parse_hhmm(sch["start"]), planner.parse_hhmm(sch["end"])
    pl = planner.plan(scheduled_profiles(cfg, sch), start, end, sch.get("strategy", "stagger"),
                      int(sch.get("first_reset_after_min", 120)), int(sch.get("min_useful_min", 60)))
    return pl, start, end, planner.parse_days(sch.get("days", "mon-fri"))


def do_ping(cfg: Config, p: Profile, model: Optional[str], force: bool, scheduled: bool) -> bool:
    s = cfg.settings
    now = time.time()
    if scheduled:
        pl, start, end, days = build_plan(cfg, schedule_settings(cfg))
        if not planner.worth_pinging(time.localtime(now), days, pl.get(p.name, []), start, end):
            _log(f"{p.name}: skipped (outside scheduled hours; the Mac was probably asleep)")
            return True
    if not p.logged_in():
        _log(f"{p.name}: not logged in, cannot open a window (cas login {p.name})")
        return False
    snap, _ = refresh(p, s)
    e = effective(snap, now)
    if not force and e.window == "active" and e.five_resets and e.five_resets - now > 10 * 60:
        _log(f"{p.name}: window already open until {when(e.five_resets)}; nothing to do")
        return True
    claude = find_claude(cfg)
    model = model or s.get("prime_model", "haiku")
    workdir = cas_home() / "ping-workdir"
    workdir.mkdir(parents=True, exist_ok=True)
    if not p.is_hub:
        prof.ensure_links(p)
    attempts = [
        [claude, "-p", PING_PROMPT, "--model", model, "--tools", "", "--no-session-persistence",
         "--strict-mcp-config", "--disable-slash-commands"],
        [claude, "-p", PING_PROMPT, "--model", model, "--no-session-persistence"],
    ]
    ok, detail, form = False, "", ""
    for form, cmd in zip(("minimal", "fallback"), attempts):
        try:
            r = subprocess.run(cmd, env=p.env(), cwd=str(workdir), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=180)
        except (OSError, subprocess.SubprocessError) as ex:
            detail = str(ex)
            continue
        if r.returncode == 0:
            ok, detail = True, (r.stdout.strip().splitlines() or [""])[-1][:60]
            break
        detail = (r.stderr.strip() or r.stdout.strip()).splitlines()[-1:] or ["exit %d" % r.returncode]
        detail = detail[0][:200]
        if re.search(r"log ?in|auth|credential", detail, re.I):
            break
    if not ok:
        _log(f"{p.name}: ping failed: {detail}")
        return False
    if e.window != "active":
        five = Window(pct=e.five_pct or 0.0, resets_at=time.time() + FIVE_HOURS)
    else:
        five = Window(e.five_pct, e.five_resets)
    record_snapshot(p.name, Snapshot(time.time(), "ping", five, Window(e.week_pct, e.week_resets),
                                     p.account_uuid, snap.extra if snap else None))
    refresh(p, s, live=s.get("live_usage") != "never")  # the ping refreshed the token: get the exact reset
    snap2 = best_snapshot(p)
    e2 = effective(snap2)
    _log(f"{p.name}: window open (model {model}, {form} flags, reply '{detail}'), resets {when(e2.five_resets)}")
    return True


def _schedule_from_args(cfg: Config, args: argparse.Namespace) -> Dict[str, Any]:
    sch = schedule_settings(cfg)
    if args.start:
        sch["start"] = args.start
    if args.end:
        sch["end"] = args.end
    if args.days:
        sch["days"] = args.days
    if args.strategy:
        sch["strategy"] = args.strategy
    if args.profiles:
        sch["profiles"] = [x.strip() for x in args.profiles.split(",") if x.strip()]
    if args.first_reset_after is not None:
        sch["first_reset_after_min"] = _minutes(args.first_reset_after)
    if args.min_useful is not None:
        sch["min_useful_min"] = _minutes(args.min_useful)
    planner.parse_hhmm(sch["start"])
    planner.parse_hhmm(sch["end"])
    planner.parse_days(sch["days"])
    return sch


def _minutes(v: str) -> int:
    v = str(v).strip().lower()
    m = re.fullmatch(r"(?:(\d+)h)?\s*(?:(\d+)m?)?", v)
    if not m or not (m.group(1) or m.group(2)):
        raise SystemExit(die(f"bad duration '{v}' (e.g. 90, 90m, 2h, 1h30m)"))
    return int(m.group(1) or 0) * 60 + int(m.group(2) or 0)


def print_plan(cfg: Config, sch: Dict[str, Any]) -> Dict[str, List[int]]:
    pl, start, end, days = build_plan(cfg, sch)
    if not pl:
        print("No logged-in profiles to schedule. Add one with: cas add <name>")
        return pl
    dn = ",".join(planner.DAY_NAMES[d] for d in days)
    print(f"Work hours {sch['start']}–{sch['end']} on {dn} · strategy {sch.get('strategy')} · "
          f"first reset {sch.get('first_reset_after_min')} min after start · model {cfg.settings.get('prime_model')}")
    print()
    print(planner.render(pl, start, end))
    print()
    for name, anchors in pl.items():
        wins = ", ".join(f"{planner.fmt_hhmm(a)}–{planner.fmt_hhmm(a + planner.WINDOW_MIN)}" for a in anchors)
        print(f"  {name}: windows {wins or '-'}")
    return pl


def cmd_plan(args: argparse.Namespace) -> int:
    cfg = Config.load()
    print_plan(cfg, _schedule_from_args(cfg, args))
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.action == "remove":
        gone = launchd.remove_all() if launchd.supported() else []
        print("Removed: " + (", ".join(gone) if gone else "nothing scheduled"))
        return 0
    if args.action == "show":
        inst = launchd.installed() if launchd.supported() else {}
        if not inst:
            print("No pings scheduled. Preview a plan with `cas plan`, install it with `cas schedule apply`.")
            return 0
        for name, entries in inst.items():
            byday: Dict[str, List[str]] = {}
            for e in entries:
                byday.setdefault(f"{e['Hour']:02d}:{e['Minute']:02d}", []).append(planner.DAY_NAMES[e["Weekday"] % 7])
            print(f"{name}: " + "; ".join(f"{t} ({','.join(d)})" for t, d in sorted(byday.items())))
        print(f"Logs: {cas_home() / 'logs'}")
        return 0

    sch = _schedule_from_args(cfg, args)
    cfg.settings["schedule"] = sch
    pl = print_plan(cfg, sch)
    if not pl:
        return 1
    if not launchd.supported():
        print("\nlaunchd is macOS only. Add these lines with `crontab -e` instead:")
        _, _, _, days = build_plan(cfg, sch)
        for name, anchors in pl.items():
            for e in planner.calendar_entries(anchors, days):
                print(f"{e['Minute']} {e['Hour']} * * {e['Weekday']} {shlex.quote(str(SCRIPT))} ping {name} --scheduled")
        cfg.save()
        return 0
    if not args.yes and not confirm("\nInstall these pings as launchd jobs?", default=True):
        return 1
    cfg.save()
    _, _, _, days = build_plan(cfg, sch)
    python = shutil.which("python3") or sys.executable
    claude = find_claude(cfg)
    path_env = ":".join(dict.fromkeys([os.path.dirname(claude), os.path.dirname(python), str(home() / ".local/bin"),
                                       "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]))
    for name, anchors in pl.items():
        entries = planner.calendar_entries(anchors, days)
        if entries:
            path = launchd.install(name, entries, python, str(SCRIPT), path_env)
            print(f"✓ {name}: {len(entries)} pings/week → {path.name}")
    for name in set(launchd.installed()) - set(pl):
        launchd.remove(name)
        print(f"✓ removed old schedule for {name}")
    first = min((a for v in pl.values() for a in v), default=None)
    if first is not None:
        wake = planner.fmt_hhmm(first - 3)
        print(f"\nIf the Mac sleeps at {planner.fmt_hhmm(first)}, the ping runs when it wakes. To wake it on time "
              f"(needs admin, plugged in):\n  sudo pmset repeat wakeorpoweron MTWRFSU {wake}:00")
    return 0


# --------------------------------------------------------------------------- init / doctor

def cas_command() -> str:
    """How Claude Code should call cas (absolute interpreter: the status line may run without your PATH)."""
    python = shutil.which("python3") or sys.executable
    return f"{shlex.quote(python)} {shlex.quote(str(SCRIPT))}"


def install_statusline(cfg: Config) -> str:
    path = hub_dir() / "settings.json"
    try:
        data = read_json_strict(path) or {}
    except Unreadable:
        return f"skipped status line: {path} is not valid JSON"
    if not isinstance(data, dict):
        return f"skipped status line: {path} is not a JSON object"
    want = f"{cas_command()} statusline"
    cur = data.get("statusLine")
    cur_cmd = cur.get("command", "") if isinstance(cur, dict) else ""
    if cur_cmd == want:
        return "status line already installed"
    ours = "bin/cas" in cur_cmd and cur_cmd.rstrip().endswith("statusline")
    if cur_cmd and not ours:
        cfg.settings["statusline_chain"] = cur_cmd
    if path.exists():
        backups_dir().mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, backups_dir() / f"settings.json.before-cas.{time.strftime('%Y%m%d-%H%M%S')}")
    data["statusLine"] = {"type": "command", "command": want, "padding": 0}
    write_json_atomic(path, data)
    return "status line installed" + (" (your previous one still runs first)" if cur_cmd and not ours else "")


ACCOUNTS_MD = """---
description: What's left on each Claude account (5-hour, weekly, limit resets) and which to use next
allowed-tools: Bash({cmd} status:*)
---
<!-- {marker} -->
!`{cmd} status --plain`

Above is the usage of my Claude accounts from claude-account-switcher (`*` marks the account this \
session runs on). In at most five short lines tell me how much this account has left and when it \
resets, which account to use next, and whether a limit reset is available. Do not use a limit reset \
and do not run any command. If this account is at or near its limit, remind me that I can exit and \
run `cas handoff` to continue this conversation on the next account, or `cas reset` to use a limit \
reset (Claude Code asks me to confirm).
"""


def install_command() -> str:
    d = hub_dir() / "commands"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "accounts.md"
    if f.exists() and MARKER not in f.read_text(encoding="utf-8", errors="replace"):
        return f"skipped /accounts: {f} exists and isn't ours"
    on_path = shutil.which("cas")
    cmd = "cas" if on_path and Path(on_path).resolve() == SCRIPT else str(SCRIPT)
    write_text_atomic(f, ACCOUNTS_MD.format(cmd=cmd, marker=MARKER), 0o644)
    return "/accounts command installed"


def link_cli() -> str:
    bindir = home() / ".local" / "bin"
    link = bindir / "cas"
    if link.is_symlink() and Path(os.path.realpath(link)) == SCRIPT:
        return f"{link} already points here"
    if link.exists() or link.is_symlink():
        return f"skipped: {link} exists and is something else"
    bindir.mkdir(parents=True, exist_ok=True)
    os.symlink(str(SCRIPT), str(link))
    on_path = str(bindir) in os.environ.get("PATH", "").split(":")
    return f"linked {link}" + ("" if on_path else f" (add {bindir} to your PATH)")


def cmd_init(args: argparse.Namespace) -> int:
    cfg = Config.load()
    if args.name and args.name.lower() != cfg.hub().name:
        rc = rename_profile(cfg, cfg.hub().name, args.name.lower())
        if rc:
            return rc
        cfg = Config.load()
    cfg.settings["claude_bin"] = find_claude(cfg)
    hub = cfg.hub()
    steps = [link_cli()]
    if not args.no_statusline:
        steps.append(install_statusline(cfg))
    if not args.no_commands:
        steps.append(install_command())
    cfg.save()
    for s in steps:
        print(f"· {s}")
    print(f"· hub profile '{hub.name}' = ~/.claude ({hub.email or 'not logged in'})")
    print()
    print("Next:")
    print("  cas add <name>     log in another account (its own profile, shared memory)")
    print("  cas status         what's left on every account")
    print("  cas run            start Claude Code on the best account")
    print("  cas plan           preview a schedule that opens 5-hour windows before you start working")
    return 0


def uninstall(args: argparse.Namespace) -> int:
    cfg = Config.load()
    path = hub_dir() / "settings.json"
    try:
        data = read_json_strict(path) or {}
    except Unreadable:
        data = None
    if isinstance(data, dict):
        sl = data.get("statusLine")
        cur = sl.get("command", "") if isinstance(sl, dict) else ""
        if "bin/cas" in cur:
            chain = cfg.settings.get("statusline_chain")
            if chain:
                data["statusLine"] = {"type": "command", "command": chain}
            else:
                data.pop("statusLine", None)
            write_json_atomic(path, data)
            print("· status line removed" + (" (previous one restored)" if chain else ""))
    if share_hooks_installed() or cfg.settings.get("shared_store"):
        print("· " + set_share_hooks(False))
    f = hub_dir() / "commands" / "accounts.md"
    if f.exists() and MARKER in f.read_text(encoding="utf-8", errors="replace"):
        f.unlink()
        print("· /accounts removed")
    if launchd.supported():
        for n in launchd.remove_all():
            print(f"· schedule removed: {n}")
    link = home() / ".local" / "bin" / "cas"
    if link.is_symlink() and Path(os.path.realpath(link)) == SCRIPT:
        link.unlink()
        print(f"· {link} removed")
    print(f"Profiles and logins are kept in {cas_home()}; `cas remove <name> --purge` deletes one.")
    return 0


def keychain_has(p: Profile) -> Optional[bool]:
    if sys.platform != "darwin":
        return None
    from .config import keychain_account

    r = subprocess.run(["security", "find-generic-password", "-a", keychain_account(), "-s", p.keychain_service],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return r.returncode == 0


def cmd_doctor(args: argparse.Namespace) -> int:
    cfg = Config.load()
    style = make_style()
    ok, bad = style.good("✓"), style.bad("✗")
    problems = 0
    try:
        claude = find_claude(cfg)
        v = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=20).stdout.strip()
        print(f"{ok} claude: {claude} ({v})")
    except SystemExit:
        print(f"{bad} claude not found")
        problems += 1
    print(f"{ok} python: {sys.executable} ({sys.version.split()[0]})")
    on_path = shutil.which("cas")
    print(f"{ok if on_path else bad} cas on PATH: {on_path or 'no (run cas init)'}")
    try:
        st = read_json_strict(hub_dir() / "settings.json") or {}
    except Unreadable:
        st = {}
        print(f"{bad} {hub_dir() / 'settings.json'} is not valid JSON")
        problems += 1
    slc = st.get("statusLine") if isinstance(st, dict) else None
    sl = slc.get("command", "") if isinstance(slc, dict) else ""
    print(f"{ok if 'bin/cas' in sl else bad} status line: {'installed' if 'bin/cas' in sl else 'not installed (cas init)'}")
    seen: Dict[str, str] = {}
    for p in cfg.profile_list():
        label = f"profile {p.name} ({p.dir})"
        if not p.dir.exists():
            print(f"{bad} {label}: directory missing")
            problems += 1
            continue
        issues = prof.link_problems(p)
        if not p.logged_in():
            issues.append("not logged in (cas login %s)" % p.name)
        kc = keychain_has(p)
        if p.logged_in() and kc is False and not (p.dir / ".credentials.json").exists():
            issues.append("no stored credentials found")
        if p.account_uuid and p.account_uuid in seen:
            issues.append(f"same account as {seen[p.account_uuid]}")
        if p.account_uuid:
            seen[p.account_uuid] = p.name
        if issues:
            problems += 1
            print(f"{bad} {label}: " + "; ".join(issues))
        else:
            print(f"{ok} {label}: {p.email} ({p.plan})")
    if launchd.supported():
        inst = launchd.installed()
        print(f"{ok} schedule: " + (", ".join(f"{n} ({len(e)}/week)" for n, e in inst.items()) if inst else "none"))
    store = shared.store_path(cfg.settings)
    if store:
        hooks_ok = share_hooks_installed()
        print(f"{ok if store.is_dir() and hooks_ok else bad} machine sharing: {store}"
              + ("" if hooks_ok else " (hooks missing: cas share)"))
        problems += 0 if store.is_dir() and hooks_ok else 1
    else:
        print(f"{ok} machine sharing: off")
    if problems:
        print(f"\n{problems} problem(s). `cas sync` fixes shared links and files.")
    return 1 if problems else 0


def cmd_statusline(_args: argparse.Namespace) -> int:
    from . import statusline

    try:
        raw = sys.stdin.read()
    except OSError:
        raw = ""
    try:
        print(statusline.main(raw))
    except Exception as e:  # a broken status line must never break Claude Code
        print(f"cas: {e.__class__.__name__}")
    return 0


# --------------------------------------------------------------------------- argparse

def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="cas", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", action="version", version=f"cas {__version__}")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("init", help="install status line, /accounts command and the cas link")
    s.add_argument("--name", help="name for the existing ~/.claude account (default: default)")
    s.add_argument("--no-statusline", action="store_true")
    s.add_argument("--no-commands", action="store_true")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("add", help="create a profile for another account and log in")
    s.add_argument("name")
    s.add_argument("--email", help="prefill the login page")
    s.add_argument("--label", help="display name")
    s.add_argument("--model", help="default --model for this profile")
    s.add_argument("--no-login", action="store_true")
    s.set_defaults(fn=cmd_add)

    s = sub.add_parser("login", help="(re)log in a profile")
    s.add_argument("name")
    s.add_argument("--email")
    s.set_defaults(fn=cmd_login)

    s = sub.add_parser("remove", help="forget a profile (--purge also logs out and deletes its files)")
    s.add_argument("name")
    s.add_argument("--purge", action="store_true")
    s.add_argument("-y", "--yes", action="store_true")
    s.set_defaults(fn=cmd_remove)

    s = sub.add_parser("rename", help="rename a profile (keeps its login and files)")
    s.add_argument("old")
    s.add_argument("new")
    s.set_defaults(fn=cmd_rename)

    s = sub.add_parser("set", help="change a profile's label, model or enabled state")
    s.add_argument("name")
    s.add_argument("--label")
    s.add_argument("--model")
    s.add_argument("--enable", action="store_true")
    s.add_argument("--disable", action="store_true")
    s.add_argument("--rename-hub", metavar="NEW")
    s.set_defaults(fn=cmd_set)

    s = sub.add_parser("status", aliases=["list", "ls"], help="what's left on every account")
    s.add_argument("--live", action="store_true", help="query the usage endpoint for every account now")
    s.add_argument("--no-live", action="store_true", help="only use cached data")
    s.add_argument("--json", action="store_true")
    s.add_argument("--plain", action="store_true", help="no colors")
    s.set_defaults(fn=cmd_status)

    sub.add_parser("run", help="start Claude Code on an account (cas run --help)")

    s = sub.add_parser("handoff", help="resume this directory's last conversation on another account")
    s.add_argument("profile", nargs="?")
    s.add_argument("--session", help="conversation id (default: latest here)")
    s.add_argument("-y", "--yes", action="store_true", help="later handoffs without asking")
    s.add_argument("claude_args", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("reset", help="use a limit reset, after showing what's left (you confirm in Claude Code)")
    s.add_argument("profile", nargs="?")
    s.add_argument("--kind", choices=["session", "weekly", "full"], help="preselect a reset kind")
    s.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last conversation here")
    s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("pick", help="print the best profile name (for scripts)")
    s.add_argument("--exclude", action="append")
    s.set_defaults(fn=cmd_pick)

    s = sub.add_parser("env", help="print shell exports to use a profile with plain `claude`")
    s.add_argument("profile")
    s.set_defaults(fn=cmd_env)

    s = sub.add_parser("sync", help="repair shared links, sync settings/CLAUDE.md/MCP servers and machine memory")
    s.add_argument("profile", nargs="?")
    s.add_argument("--quick", action="store_true", help="machines: only memory")
    s.add_argument("--hook", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_sync)

    s = sub.add_parser("share", help="share memory with your other machines through a folder they keep in sync")
    s.add_argument("--store", help="a folder your machines keep in sync")
    s.add_argument("--projects-root", help="projects at the same path relative to this folder share memory (default ~)")
    s.add_argument("--no-hooks", action="store_true", help="don't sync automatically from Claude Code")
    s.add_argument("--status", action="store_true")
    s.add_argument("--off", action="store_true")
    s.set_defaults(fn=cmd_share)

    s = sub.add_parser("ping", help="open a 5-hour window now with a tiny message")
    s.add_argument("profile", help="profile name or 'all'")
    s.add_argument("--model")
    s.add_argument("--force", action="store_true", help="ping even if a window is already open")
    s.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_ping)

    def sched_opts(x: argparse.ArgumentParser) -> None:
        x.add_argument("--start", help="work start, HH:MM")
        x.add_argument("--end", help="work end, HH:MM")
        x.add_argument("--days", help="mon-fri, daily, weekends, mon,wed,fri ...")
        x.add_argument("--strategy", choices=["stagger", "align"])
        x.add_argument("--profiles", help="comma-separated (default: all logged-in)")
        x.add_argument("--first-reset-after", help="first window resets this long after start (default 2h)")
        x.add_argument("--min-useful", help="skip windows overlapping work hours less than this (default 60m)")

    s = sub.add_parser("plan", help="preview when to open each account's window")
    sched_opts(s)
    s.set_defaults(fn=cmd_plan)

    s = sub.add_parser("schedule", help="install/show/remove the window-opening pings (launchd)")
    s.add_argument("action", choices=["apply", "show", "remove"], nargs="?", default="show")
    s.add_argument("-y", "--yes", action="store_true")
    sched_opts(s)
    s.set_defaults(fn=cmd_schedule)

    s = sub.add_parser("doctor", help="check the setup")
    s.set_defaults(fn=cmd_doctor)

    s = sub.add_parser("uninstall", help="remove status line, /accounts and schedules (keeps profiles)")
    s.set_defaults(fn=uninstall)

    s = sub.add_parser("statusline", help="status line command (Claude Code runs it; reads JSON on stdin)")
    s.set_defaults(fn=cmd_statusline)
    return ap


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "run":
        return cmd_run(argv[1:])
    ap = build_parser()
    args = ap.parse_args(argv)
    if not getattr(args, "fn", None):
        ap.print_help()
        return 0
    try:
        return int(args.fn(args) or 0)
    except UnknownProfile as e:
        return die(str(e))
    except KeyboardInterrupt:
        return 130
