"""cas: run Claude Code on several accounts with one shared memory.

  cas init                     set up (status line, /accounts command, `cas` on PATH)
  cas add <name>               new profile + browser login for another account
  cas status                   what's left on every account: 5-hour, weekly, limit resets, credits
  cas run [name|auto] [args]   start Claude Code on an account; offers a handoff at the limit
  cas handoff [name]           resume the last conversation here on another account
  cas send / cas receive       move a conversation to another machine (through the `cas share` folder)
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
import tarfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import __version__, agents, keychain, launchd, picker, planner, resets, shared, transfer
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
from .current import get_current, set_current
from .usage import (
    FIVE_HOURS,
    Effective,
    Snapshot,
    Window,
    best_snapshot,
    effective,
    extra_summary,
    live_due,
    load_state,
    record_snapshot,
    refresh,
    save_state,
)

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "cas"
MARKER = "managed by claude-account-switcher"
PING_PROMPT = "Reply with exactly one word: ok"
PING_SETTLE_WAITS = (0, 3, 5, 10)  # seconds before each try at reading the window a ping opened

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
    lt, ln = time.localtime(ts + 30), time.localtime(now)  # to the nearest minute
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
            if agents.limit_error(obj):
                return True
    return False


def agents_to_continue(cfg: Config, session: Optional[str], since: Optional[float] = None) -> List[agents.Agent]:
    """The agents a closed session stopped (by its end or by the limit), for the next one to continue.

    `since`: when that session started; unknown, an hour before the conversation's last message."""
    if not session or not cfg.settings.get("resume_agents", True):
        return []
    t = transcript_path(session)
    if t is None:
        return []
    try:
        found = agents.session_agents(t)
        if found and since is None:
            since = agents.last_at(t) - agents.HANDOFF_WINDOW_S
        return agents.cut_off(found, since or 0)
    except OSError:
        return []


def agents_prompt(cut: Sequence[agents.Agent], account: Optional[str], style: Style,
                  machine: Optional[str] = None) -> List[str]:
    """The opening message that has Claude continue `cut` on `account`, or here after a move from
    `machine` (none when there are none)."""
    if not cut:
        return []
    n = len(cut)
    where = "here" if machine else f"on {account}"
    print(style.dim(f"· {n} agent{'s' if n != 1 else ''} stopped before finishing; Claude continues "
                    f"{'them' if n != 1 else 'it'} {where}: " + ", ".join(a.name() for a in cut)))
    return [agents.resume_prompt(cut, account, machine)]


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

    ordered, blocked = picker.rank([(r.profile, r.eff) for r in rows], cfg.settings,
                                   prefer=active_profile(cfg).name)
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
        print(style.dim("Limit resets are only used when you ask: `cas reset <profile>` opens /limit-reset or "
                        "claude.ai's usage page, and you confirm there."))
    if shell_hook_installed():
        p, where = get_current(cfg)
        print(style.dim(f"A fresh `claude` starts on {(p or cfg.hub()).name}, the account cas last used"
                        + (f" on {where}" if where else "") + " (`cas run` picks the best one)."))
    waiting = transfer.pending(cfg.settings) if cfg.settings.get("shared_store") else []
    if waiting:
        n = len(waiting)
        print(style.c("36", f"{n} conversation{'s' if n != 1 else ''} sent from your other machines "
                            f"wait{'s' if n == 1 else ''} here: cas receive --list"))


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
    if reset_opt:
        r, g = reset_opt
        via_cc = bool(r.resets and r.resets.via_claude_code)
        if reset_unsure:
            label = f"check for a limit reset on {r.profile.name}: " + (
                "opens /limit-reset in Claude Code, which shows what you have and asks before using one" if via_cc
                else "opens claude.ai's usage page, where you see them and click Reset yourself")
        else:
            desc = g.name if g else "a limit reset"
            extra = f", {g.left} left" + (f", use by {when(g.ends_at, now)}" if g.ends_at else "") if g else ""
            label = f"use {desc} on {r.profile.name}{extra}: " + (
                "opens Claude Code's /limit-reset, you confirm there" if via_cc
                else "opens claude.ai's usage page, you click Reset there")
        options.append((Action("reset", r.profile, g), label))
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
    ordered, _ = picker.rank([(r.profile, r.eff) for r in rows], cfg.settings, prefer=active_profile(cfg).name)
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


def refresh_in_background(cfg: Config, skip: Sequence[str] = (), now: Optional[float] = None) -> List[str]:
    """Bring out-of-date usage up to date without waiting for it (`live_usage` decides what is).

    Runs when a session starts and from the status line during it: the status line only reads what
    cas has recorded, so its `next:` would otherwise show "?" or a percentage from long ago. An
    account is fetched at most once per `live_max_age_s`, even when its fetch fails.
    """
    now = time.time() if now is None else now
    gap = float(cfg.settings.get("live_max_age_s", 300))
    spawned = load_state("_refresh")
    due = [q.name for q in cfg.profile_list() if q.enabled and q.logged_in() and q.name not in skip
           and live_due(best_snapshot(q), cfg.settings, now) and now - float(spawned.get(q.name) or 0) >= gap]
    if not due:
        return []
    spawned.update({n: now for n in due})
    save_state("_refresh", spawned)
    log = cas_home() / "logs" / "refresh.log"
    try:
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "w") as fh:  # only the last run: this happens every time a session starts
            subprocess.Popen([sys.executable, str(SCRIPT), REFRESH_CMD, *due], stdin=subprocess.DEVNULL,
                             stdout=fh, stderr=subprocess.STDOUT, start_new_session=True, env=os.environ.copy())
    except OSError:
        return []
    return due


REFRESH_CMD = "_refresh"  # internal: the background half of refresh_in_background


def cmd_refresh(names: Sequence[str]) -> int:
    cfg = Config.load()
    for n in names:
        p = cfg.profiles.get(n)
        if p is None or not p.logged_in():
            continue
        _, err = refresh(p, cfg.settings)  # checks again: another process may have just done it
        if err:
            _log(f"{p.name}: {err}")
    return 0


def ensure_login(p: Profile, style: Style, interactive: bool, out: Any = None) -> None:
    """Before Claude Code starts on `p`: one login per account, and one Claude Code can use.

    Over SSH the macOS Keychain is usually locked, and a login kept there can't be read or
    renewed (see keychain.py). A stale Keychain copy is removed without asking; a login only
    the locked Keychain holds needs it unlocked, which macOS asks the password for.
    """
    out = out or sys.stdout
    try:
        login, actions = keychain.settle(p)
    except Exception:  # never stop Claude Code from starting over this
        return
    for a in actions:
        print(style.dim(f"· {p.name}: {a}"), file=out)
    if login is None or not login.stranded:
        return
    print(style.warn(f"{p.name}'s login is in the macOS Keychain, which is locked (usual over SSH). Claude Code "
                     "can't read or renew it there, so it would ask you to log in again."), file=out)
    if not (interactive and sys.stdin.isatty()):
        print(style.dim("  Unlock it with `security unlock-keychain`, or log in again: a login made while the "
                        "Keychain is locked goes to .credentials.json and keeps working."), file=out)
        return
    if not confirm("Unlock the Keychain now? macOS asks for your Mac password.", default=True):
        print(style.dim("  Starting anyway. If Claude Code asks, /login: with the Keychain locked the login goes to "
                        ".credentials.json, and cas drops the old Keychain copy next time."), file=out)
        return
    if not keychain.unlock():
        print(style.warn("  The Keychain is still locked."), file=out)
        return
    print(style.good("✓ Keychain unlocked"), file=out)
    how = keychain.autolock()
    if how:
        print(style.dim(f"  It locks again {how}. A session still open then can't renew its login and asks you "
                        "to log in again."), file=out)
        if confirm("Keep it unlocked until you log out or restart the Mac? (security set-keychain-settings)",
                   default=False):
            print(style.good("✓ The Keychain no longer locks by itself") if keychain.keep_unlocked()
                  else style.warn("  Could not change the Keychain settings."), file=out)


def run_claude(claude: str, args: List[str], p: Profile) -> int:
    try:
        return subprocess.call([claude, *args], env=p.env())
    except KeyboardInterrupt:
        return 130


def run_loop(cfg: Config, target: str, claude_args: List[str], mode: str, style: Style,
             prompt: Sequence[str] = ()) -> int:
    if in_claude_code():
        return die("you are inside a Claude Code session; exit it first, then run this from your terminal")
    claude = find_claude(cfg)
    cwds = cwd_candidates()
    rc = 0
    pending: List[str] = list(prompt)  # one-shot trailing prompt, e.g. "/limit-reset"
    limited: List[str] = []  # accounts that hit their limit during this run: never hand back to them
    reset_offered: List[str] = []  # offer a limit reset at most once per account per run
    while True:
        p, extra = resolve_target(cfg, target, style)
        if p is None:
            return rc
        for a in prof.prepare(p, cfg.profile_list(), cfg.settings.get("sync_mcp", True)):
            print(style.dim(f"· {a}"))
        ensure_login(p, style, interactive=True)
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
        set_current(cfg, p, started)
        primed = prime_others(cfg, p, style, started)
        refresh_in_background(cfg, skip=primed, now=started)
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
        cut = agents_to_continue(cfg, session, since=started - 5) if action.profile else []
        if action.kind == "switch" and action.profile:
            target, claude_args = action.profile.name, carry_flags(claude_args) + resume
            pending = agents_prompt(cut, target, style)
            continue
        if action.kind == "reset" and action.profile:
            target = action.profile.name
            reset_offered.append(target)
            rs_now = resets.best_status(action.profile)
            if rs_now is not None and rs_now.via_claude_code:
                claude_args = carry_flags(claude_args) + (["--resume", session] if session else [])
                pending = resets.limit_reset_arg(action.grant)
                print(style.dim("Opening /limit-reset in Claude Code. Nothing is used until you confirm there "
                                "(if no dialog appears, type /limit-reset)."))
                agents_hint(cut, style)
            elif use_reset_on_web(action.profile, cfg.settings, style):
                claude_args = carry_flags(claude_args) + (["--resume", session] if session else ["--continue"])
                pending = agents_prompt(cut, target, style)
            else:
                return rc
            if target in limited:
                limited.remove(target)  # it may have room again after the reset
            continue
        return rc


def agents_hint(cut: Sequence[agents.Agent], style: Style) -> None:
    """The session opens on /limit-reset, not on a prompt: say how to get the stopped agents going."""
    if cut:
        n = len(cut)
        print(style.dim(f"{n} agent{'s' if n != 1 else ''} stopped before finishing "
                        f"({', '.join(a.name() for a in cut)}). After the reset, ask Claude to continue "
                        f"{'them' if n != 1 else 'it'} with SendMessage."))


def use_reset_on_web(p: Profile, settings: Dict[str, Any], style: Style) -> bool:
    """Resets that Claude Code can't use are used on claude.ai: open the page, the user clicks."""
    print(f"Opening {resets.WEB_USAGE_URL}. Check it's signed in as {p.email or p.name}, then click "
          "“Reset for free” on the limit reset you want.")
    try:
        subprocess.call(["open", resets.WEB_USAGE_URL], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:
        pass
    if not confirm("Did you use the reset?", default=False):
        print("Nothing used.")
        return False
    refresh(p, settings, live=True)  # a sharp drop in usage marks the reset as used
    return True


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
    style = make_style()
    waiting_hint(cfg, style)
    try:
        return run_loop(cfg, target, rest, mode, style)
    except UnknownProfile as e:
        return die(str(e))


# --------------------------------------------------------------------------- plain `claude`

# `claude` subcommands that manage what cas spreads from ~/.claude to every profile (MCP servers,
# plugins) or the install itself: they keep running on ~/.claude.
HUB_SUBCOMMANDS = {"mcp", "plugin", "plugins", "install", "update", "upgrade", "doctor"}


def active_profile(cfg: Config) -> Profile:
    """The account a fresh session starts on: the one cas last started or handed off to, on any of
    your machines when `cas share` is on (current.py)."""
    p, _ = get_current(cfg)
    return p or cfg.hub()


def cmd_use(args: argparse.Namespace) -> int:
    """Make `profile` the current account without starting a session (on every machine, with cas share)."""
    cfg = Config.load()
    p = cfg.get(args.profile)
    if not p.enabled:
        return die(f"{p.name} is turned off (cas set {p.name} --enable)")
    if not p.logged_in():
        return die(f"{p.name} is not logged in (cas login {p.name})")
    set_current(cfg, p)
    where = " on all your machines" if cfg.settings.get("shared_store") else ""
    print(f"A fresh `claude` now starts on {p.name}{where}; sessions already open stay on their account.")
    return 0


def cmd_claude(argv: List[str]) -> int:
    """Plain `claude`, through the shell function `cas init` installs: start on the current account.

    No handoff loop and no background pings (that's `cas run`): the profile is prepared the same
    way, then claude replaces this process. An account chosen already (CLAUDE_CONFIG_DIR or
    CAS_PROFILE set, e.g. by `cas env` or inside a session) is kept.
    """
    cfg = Config.load()
    claude = find_claude(cfg)
    args = list(argv)
    env = dict(os.environ)
    sub = args[0] if args and args[0] in CLAUDE_SUBCOMMANDS else None
    chosen = env.get("CLAUDE_CONFIG_DIR") or env.get("CAS_PROFILE") or in_claude_code()
    if not chosen and sub not in HUB_SUBCOMMANDS:
        talk = sub is None and sys.stdin.isatty() and sys.stderr.isatty() \
            and not (has_flag(args, "-p") or has_flag(args, "--print"))
        style = Style(talk and not os.environ.get("NO_COLOR"))
        p = active_profile(cfg)
        try:
            for a in prof.prepare(p, cfg.profile_list(), cfg.settings.get("sync_mcp", True)):
                if talk:
                    print(style.dim(f"· {a}"), file=sys.stderr)
        except Exception as e:  # a failed sync must never stop claude from starting
            if talk:
                print(style.dim(f"· sync failed: {e}"), file=sys.stderr)
        if talk:
            ensure_login(p, style, interactive=True, out=sys.stderr)
        else:
            try:
                keychain.settle(p)
            except Exception:
                pass
        if p.model and sub is None and not has_flag(args, "--model"):
            args = ["--model", p.model] + args
        env = p.env(env)
        if talk:
            refresh_in_background(cfg)  # for the status line, which only reads what cas recorded
            waiting_hint(cfg, style, out=sys.stderr)
            print(style.dim(f"→ Claude Code on {p.name} ({p.display()})"), file=sys.stderr)
    try:
        os.execve(claude, [claude, *args], env)
    except OSError as e:
        return die(f"could not start {claude}: {e}")
    return 0  # unreachable: execve only returns by raising


def cmd_handoff(args: argparse.Namespace) -> int:
    cfg = Config.load()
    style = make_style()
    session = args.session or choose_session(cfg, cwd_candidates(), style)
    if not session:
        return die("no conversation found for this directory; run `cas run` instead")
    t = transcript_path(session)
    if t and time.time() - last_activity(t) < 90 and \
            not confirm(f"{session[:8]} was active less than 90s ago; if it's still open elsewhere, both would "
                        "write to it. Continue?", default=False):
        return 1
    cut = [] if args.no_agents else agents_to_continue(cfg, session)
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
                agents_hint(cut, style)
                return run_loop(cfg, a.profile.name, ["--resume", session] + resets.limit_reset_arg(a.grant),
                                "ask", style)
            if a.kind not in ("switch", "start") or not a.profile:
                return 0
            target = a.profile.name
    mode = "auto" if args.yes else cfg.settings.get("handoff", "ask")
    return run_loop(cfg, target, ["--resume", session] + list(args.claude_args or []), mode, style,
                    prompt=agents_prompt(cut, target, style))


# --------------------------------------------------------------------------- moving to another machine

NO_STORE = ("a conversation moves to another machine through the folder `cas share` uses; turn that on first: "
            "cas share --store <a folder your machines keep in sync>")


def last_activity(t: Path) -> float:
    """When a conversation, or one of its unfinished agents, last wrote to disk."""
    try:
        own = t.stat().st_mtime
    except OSError:
        return 0.0
    return max([own] + [a.mtime() for a in agents.session_agents(t) if not a.finished])


def short_path(p: Path) -> str:
    h, s = str(home()).rstrip("/"), str(p)
    return "~" + s[len(h):] if s == h or s.startswith(h + "/") else s


def size_text(n: int) -> str:
    return f"{n / 1e6:.1f} MB" if n >= 100_000 else f"{max(1, round(n / 1000))} kB"


def find_transcript(session: str) -> Tuple[Optional[str], Optional[Path]]:
    """(full id, transcript) of a conversation on this machine, from its id or the start of it."""
    t = transcript_path(session)
    if t:
        return session, t
    hits = {p.stem: p for p in (hub_dir() / "projects").glob(f"*/{glob_escape(session)}*.jsonl")}
    if len(hits) == 1:
        sid = next(iter(hits))
        return sid, transcript_path(sid)
    return None, None


def glob_escape(s: str) -> str:
    return re.sub(r"([*?\[])", r"[\1]", s)


def cmd_send(args: argparse.Namespace) -> int:
    """Put a conversation in the shared folder, for `cas receive` on another machine."""
    cfg = Config.load()
    style = make_style()
    store = shared.store_path(cfg.settings)
    if not store:
        return die(NO_STORE)
    inside = os.environ.get("CLAUDE_CODE_SESSION_ID") if in_claude_code() else None
    if args.session:
        session, t = find_transcript(args.session)
        if not session:
            return die(f"no conversation {args.session} on this machine")
    else:
        session = inside if inside and transcript_path(inside) else choose_session(cfg, cwd_candidates(), style)
        t = transcript_path(session) if session else None
        if not session or not t:
            return die("no conversation found for this directory (--session <id> picks one)")
    if t is None:
        return die(f"no transcript for {session} on this machine")
    still_open = session == inside
    now = time.time()
    if not still_open and now - last_activity(t) < 90 and not confirm(
            f"{session[:8]} was active less than 90s ago. If it's still open, what happens there from now on stays "
            "on this machine. Send it anyway?", default=False):
        return 1
    src = session_profile(cfg, session)
    try:
        pkg = transfer.pack(cfg.settings, session, t, label=transfer.title(t) or first_prompt(t),
                            still_open=still_open, profile=src.name if src else None,
                            account=src.account_uuid if src else None, now=now)
    except (OSError, ValueError, tarfile.TarError) as e:
        return die(f"could not send {session[:8]}: {e}")
    print(style.good("✓") + f" Sent {session[:8]} “{pkg.label}” ({size_text(int(pkg.manifest['size']))}) "
          f"to your other machines, through {short_path(store)}.")
    rel = pkg.manifest.get("project")
    root = str(cfg.settings.get("projects_root") or "~").rstrip("/")
    there = (root if rel == "." else f"{root}/{rel}") if rel is not None else pkg.cwd
    print(f"  On the other machine: cas receive. It continues there in {there}"
          + ("" if rel is not None else " (the same path)") + ", on the account cas picks.")
    if still_open:
        print(style.warn("  This session is still open: exit it now. What happens here from now on stays on this "
                         "machine (cas send again updates what you sent)."))
        busy = agents.running(agents.session_agents(t), now)
        if busy:
            n = len(busy)
            print(style.warn(f"  {n} agent{'s are' if n != 1 else ' is'} still at work; what "
                             f"{'they do' if n != 1 else 'it does'} from now on stays here too. To take it along, "
                             "wait until they finish, then cas send again."))
    if not any(m.get("host") != shared.host() for m in shared.machines(store)):
        print(style.dim("  No other machine uses this shared folder yet: run `cas share --store <the same folder>` "
                        "there first."))
    return 0


def describe_package(cfg: Config, p: transfer.Package, now: float) -> str:
    return f"{p.session[:8]}  from {p.host}, {age(now - p.sent_at)}  {short_path(p.local_dir(cfg.settings))}  {p.label}"


def choose_package(cfg: Config, pkgs: List[transfer.Package], style: Style) -> Optional[transfer.Package]:
    """The conversation to receive: the one sent for this folder, else ask among all of them."""
    here = os.path.realpath(os.getcwd())
    mine = [p for p in pkgs if os.path.realpath(str(p.local_dir(cfg.settings))) == here]
    if len(mine) == 1:
        return mine[0]
    cands = mine or pkgs
    if len(cands) == 1 or not sys.stdin.isatty():
        if len(cands) > 1:
            print(style.dim(f"{len(cands)} conversations waiting; taking the latest ({cands[0].session[:8]}). "
                            "Pick another with --session <id>."))
        return cands[0]
    now = time.time()
    print("Conversations sent from your other machines" + (" for this folder:" if mine else ":"))
    for i, p in enumerate(cands, 1):
        print(f"  [{i}] {describe_package(cfg, p, now)}")
    try:
        ans = input("Which one? (Enter = 1): ").strip()
    except EOFError:
        ans = ""
    try:
        idx = int(ans) - 1 if ans else 0
    except ValueError:
        return None
    return cands[idx] if 0 <= idx < len(cands) else None


def cmd_receive(args: argparse.Namespace) -> int:
    """Continue a conversation another machine sent (cas send): put it in place here and resume it."""
    cfg = Config.load()
    style = make_style()
    s = cfg.settings
    if not shared.store_path(s):
        return die(NO_STORE)
    now = time.time()
    pkgs, own = transfer.pending(s), transfer.pending(s, own=True)
    if args.list:
        if not pkgs and not own:
            print("No conversation is waiting in the shared folder.")
        for p in pkgs:
            print(f"  {describe_package(cfg, p, now)}")
        for p in own:
            print(style.dim(f"  {p.session[:8]}  sent from this machine {age(now - p.sent_at)}, for another one  "
                            f"{p.label}"))
        return 0
    if in_claude_code() and not args.discard:
        return die("you are inside a Claude Code session; exit it first, then run this from your terminal")
    pool = pkgs + own if args.discard else pkgs  # what you sent yourself can be withdrawn too
    if args.session:
        hits = [p for p in pool if p.session.startswith(args.session)]
        if len(hits) != 1:
            return die(f"no conversation {args.session} is waiting" if not hits
                       else f"{args.session} matches several; give more of its id")
        pkg: Optional[transfer.Package] = hits[0]
    elif not pool:
        if own:
            n = len(own)
            return die(f"nothing is waiting from your other machines; the {n if n > 1 else ''}"
                       f"{' conversations' if n > 1 else 'conversation'} sent from this one "
                       f"wait{'s' if n == 1 else ''} for another machine: run cas receive there")
        return die("nothing is waiting from your other machines. On the machine you're leaving: cas send")
    else:
        pkg = choose_package(cfg, pool, style)
    if pkg is None:
        return 1
    if args.discard:
        transfer.discard(pkg)
        print(f"Discarded {pkg.session[:8]} “{pkg.label}”, sent from {pkg.host}. Its own machine still has it.")
        return 0
    problem = pkg.problem()
    if problem:
        return die(f"{pkg.session[:8]} from {pkg.host}: {problem}")
    where = pkg.local_dir(s)
    here = Path(os.getcwd())
    if not where.is_dir():
        print(f"{pkg.session[:8]} ran in {pkg.cwd} on {pkg.host}; this machine has no {short_path(where)}.")
        if not confirm(f"Continue it here, in {short_path(here)}?", default=False):
            return 1
        where = here
    print(style.dim(f"{pkg.session[:8]} from {pkg.host}, sent {age(now - pkg.sent_at)}: {pkg.label}"))
    if os.path.realpath(str(where)) != os.path.realpath(str(here)):
        print(style.dim(f"→ in {short_path(where)}"))
    if pkg.manifest.get("open"):
        print(style.dim(f"  (sent from inside its session: anything done after that stays on {pkg.host})"))
    mine = hub_dir() / "projects" / sanitize(os.path.realpath(str(where))) / f"{pkg.session}.jsonl"
    if mine.exists() and now - last_activity(mine) < 90 and not confirm(
            f"This machine's copy of {pkg.session[:8]} was active less than 90s ago; if it's open here, both would "
            "write to it. Replace it anyway (the current one goes to a backup)?", default=False):
        return 1
    try:
        _, acts = transfer.unpack(s, pkg, str(where))
    except (OSError, ValueError, tarfile.TarError) as e:
        return die(f"could not receive {pkg.session[:8]}: {e}")
    for a in acts:
        print(style.dim(f"· {a}"))
    os.chdir(where)
    os.environ["PWD"] = str(where)
    cut = [] if args.no_agents else agents_to_continue(cfg, pkg.session)
    mode = "auto" if args.yes else s.get("handoff", "ask")
    return run_loop(cfg, args.profile or "auto", ["--resume", pkg.session] + list(args.claude_args or []), mode,
                    style, prompt=agents_prompt(cut, None, style, machine=pkg.host))


def waiting_hint(cfg: Config, style: Style, out: Any = None) -> None:
    """Say that another machine sent a conversation for this folder (cas receive continues it)."""
    if not cfg.settings.get("shared_store"):
        return
    try:
        pkgs = transfer.waiting_here(cfg.settings, os.getcwd())
    except Exception:  # an offline shared folder must not get in the way of starting claude
        return
    if pkgs:
        p = pkgs[0]
        more = f" (and {len(pkgs) - 1} more)" if len(pkgs) > 1 else ""
        print(style.warn(f"· {p.host} sent a conversation for this folder {age(time.time() - p.sent_at)}: "
                         f"“{p.label}”{more}. To continue it: exit, then cas receive"), file=out)


def cmd_reset(args: argparse.Namespace) -> int:
    cfg = Config.load()
    style = make_style()
    now = time.time()
    p = cfg.get(args.profile) if args.profile else active_profile(cfg)
    if args.set is not None:
        ends = None
        if args.expires:
            try:
                fmt = "%Y-%m-%d %H:%M" if " " in args.expires.strip() else "%Y-%m-%d"
                ends = time.mktime(time.strptime(args.expires.strip(), fmt))
                ends += 0 if " " in args.expires.strip() else 86399  # a date means: until the end of that day
            except ValueError:
                return die("--expires takes YYYY-MM-DD or 'YYYY-MM-DD HH:MM'")
        try:
            resets.set_manual(p, args.set, args.kind or "full", ends)
        except ValueError as e:
            return die(str(e))
        rs = resets.best_status(p, now)
        print(f"{p.name}: limit resets → {rs.summary(now) if rs else 'none'}")
        return 0
    snap, err = refresh(p, cfg.settings, live=True)
    row = Row(p, snap, effective(snap, now), resets.best_status(p, now), err)
    print(f"{style.bold(p.name)} ({p.display()})")
    print("  " + left_line(row, style, now))
    rs = row.resets
    via_cc = bool(rs and rs.via_claude_code)
    if err:
        print(style.dim(f"  live data unavailable: {err}"))
    unknown = rs is None or rs.maybe(now)
    if not unknown and not rs.has_any(now):
        print("No limit reset on this account: " + rs.summary(now) + ".")
        return 1
    if unknown:
        print("  cas can't see this account's limit resets (the server doesn't share them with it). " + (
            "/limit-reset in Claude Code shows them." if via_cc else
            f"claude.ai → Settings → Usage shows them; record them with: cas reset {p.name} --set N "
            "--expires YYYY-MM-DD"))
    grants = rs.usable(now) if rs else []
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
    week_low = row.eff.week_pct is not None and row.eff.week_pct >= 85
    if week_low:
        print(style.warn(f"  Only {100 - row.eff.week_pct:.0f}% of the weekly limit is left "
                         f"(until {when(row.eff.week_resets, now)}): a full or weekly reset clears that too."))
    elif row.eff.five_pct is not None and row.eff.five_pct < float(cfg.settings.get("switch_threshold", 90)):
        print(style.warn(f"  This account still has {100 - row.eff.five_pct:.0f}% of its 5-hour window; "
                         "a reset is usually worth keeping for when you hit the limit."))
    others = picker.best([(r.profile, r.eff) for r in gather(cfg)], cfg.settings, exclude={p.name})
    if others:
        print(style.dim(f"  Alternative that costs nothing: {others.profile.name} ({others.reason})."))
    if not via_cc:
        if not confirm("Open claude.ai's usage page to use one? (you click Reset there)"):
            print("Nothing used.")
            return 0
        if use_reset_on_web(p, cfg.settings, style) and args.cont:
            return run_loop(cfg, p.name, ["--continue"], cfg.settings.get("handoff", "ask"), style)
        return 0
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
        c = picker.best([(r.profile, r.eff) for r in rows], cfg.settings, prefer=active_profile(cfg).name)
        print(json.dumps({"profiles": out, "next": c.profile.name if c else None,
                          "current": active_profile(cfg).name}, indent=2))
        return 0
    style = make_style(args.plain)
    print_status(cfg, rows, style, now, mark_current=in_claude_code() or bool(os.environ.get("CAS_PROFILE")))
    return 0


def cmd_pick(args: argparse.Namespace) -> int:
    cfg = Config.load()
    rows = gather(cfg)
    c = picker.best([(r.profile, r.eff) for r in rows], cfg.settings, exclude=args.exclude or (),
                    prefer=active_profile(cfg).name)
    if not c:
        return 1
    print(c.profile.name)
    return 0


def cmd_env(args: argparse.Namespace) -> int:
    cfg = Config.load()
    p = cfg.get(args.profile) if args.profile else active_profile(cfg)
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
    try:
        for a in keychain.settle(p)[1]:
            print(f"· {a}")
    except Exception:
        pass
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
    for p in targets:
        if p.dir.exists():
            for a in keychain.settle(p)[1]:
                print(f"{p.name}: {a}")
                n += 1
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
        waiting = transfer.pending(s) + transfer.pending(s, own=True)
        print(f"Conversations waiting:       {len(waiting)}" + (" (cas receive --list)" if waiting else ""))
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
    try:
        login, fixed = keychain.settle(p)
    except Exception:
        login, fixed = None, []
    for a in fixed:
        _log(f"{p.name}: {a}")
    if login is not None and login.stranded:
        _log(f"{p.name}: its login is in the macOS Keychain, which is locked; cannot open a window "
             "(unlock it with `security unlock-keychain`)")
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
    if s.get("live_usage") != "never":
        # The ping refreshed the token: get the exact reset. The usage endpoint takes a few seconds
        # to count the ping, and a reading taken before that is dropped (record_snapshot), so ask
        # again until one shows the window.
        for wait in PING_SETTLE_WAITS:
            time.sleep(wait)
            _, err = refresh(p, s, live=True)
            cur = best_snapshot(p)
            if err or cur is None or cur.source != "ping":
                break
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

def cas_command(script: Optional[Path] = None) -> str:
    """How Claude Code should call cas (absolute interpreter: the status line may run without your PATH)."""
    python = shutil.which("python3") or sys.executable
    return f"{shlex.quote(python)} {shlex.quote(str(script or SCRIPT))}"


def statusline_command() -> str:
    try:
        data = read_json_strict(hub_dir() / "settings.json") or {}
    except Unreadable:
        return ""
    sl = data.get("statusLine") if isinstance(data, dict) else None
    return sl.get("command", "") if isinstance(sl, dict) else ""


def is_our_statusline(cmd: str) -> bool:
    return "bin/cas" in cmd and cmd.rstrip().endswith("statusline")


def install_statusline(cfg: Config, script: Optional[Path] = None) -> str:
    path = hub_dir() / "settings.json"
    try:
        data = read_json_strict(path) or {}
    except Unreadable:
        return f"skipped status line: {path} is not valid JSON"
    if not isinstance(data, dict):
        return f"skipped status line: {path} is not a JSON object"
    want = f"{cas_command(script)} statusline"
    cur = data.get("statusLine")
    cur_cmd = cur.get("command", "") if isinstance(cur, dict) else ""
    if cur_cmd == want:
        return "status line already installed"
    ours = is_our_statusline(cur_cmd)
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


def accounts_command_installed() -> bool:
    f = hub_dir() / "commands" / "accounts.md"
    return f.exists() and MARKER in f.read_text(encoding="utf-8", errors="replace")


def install_command(script: Optional[Path] = None) -> str:
    script = script or SCRIPT
    d = hub_dir() / "commands"
    d.mkdir(parents=True, exist_ok=True)
    f = d / "accounts.md"
    if f.exists() and MARKER not in f.read_text(encoding="utf-8", errors="replace"):
        return f"skipped /accounts: {f} exists and isn't ours"
    on_path = shutil.which("cas")
    cmd = "cas" if on_path and Path(on_path).resolve() == script else str(script)
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


# --------------------------------------------------------------------------- installed copy

INSTALL_MARK = "installed.json"  # in the installed copy: where it came from and when


def install_dir() -> Path:
    """Where `cas install` copies cas, so editing the source folder never changes the cas you use."""
    return home() / ".local" / "share" / "claude-account-switcher"


def source_files(src: Path) -> List[Path]:
    """What `cas install` copies, relative to the source folder: the program and nothing else."""
    return sorted(p.relative_to(src) for p in (src / "cas").glob("*.py")) + [Path("bin") / "cas"]


def git_version(src: Path) -> Optional[str]:
    try:
        head = subprocess.run(["git", "-C", str(src), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=10)
        if head.returncode != 0:
            return None
        dirty = subprocess.run(["git", "-C", str(src), "status", "--porcelain", "--", "bin", "cas"],
                               capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return head.stdout.strip() + (" + uncommitted changes" if dirty.stdout.strip() else "")


def install_differs(src: Path, dest: Path) -> List[str]:
    """Source files the installed copy lacks or has in another version."""
    out = []
    for rel in source_files(src):
        try:
            if not (dest / rel).is_file() or (src / rel).read_bytes() != (dest / rel).read_bytes():
                out.append(str(rel))
        except OSError:
            out.append(str(rel))
    return out


def point_link(target: Path) -> str:
    link = home() / ".local" / "bin" / "cas"
    if link.is_symlink() and Path(os.path.realpath(link)) == target:
        return f"{link} already points to the installed copy"
    if link.exists() and not link.is_symlink():
        return f"skipped: {link} exists and isn't a link"
    if link.is_symlink() and Path(os.path.realpath(link)).name != "cas":
        return f"skipped: {link} points to something else"
    link.parent.mkdir(parents=True, exist_ok=True)
    tmp = link.with_name(".cas.tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    os.symlink(str(target), str(tmp))
    os.replace(tmp, link)  # never a moment without `cas`
    return f"{link} → installed copy"


def repoint_setup(cfg: Config, script: Path) -> List[str]:
    """Point what cas set up (the `cas` link, status line, /accounts, scheduled pings) at `script`.
    Pieces you chose not to have (cas init --no-statusline, ...) stay out."""
    steps = [point_link(script)]
    if is_our_statusline(statusline_command()):
        steps.append(install_statusline(cfg, script))
    if accounts_command_installed():
        steps.append(install_command(script))
    if launchd.supported():
        try:
            moved = launchd.repoint(str(script))
        except (OSError, RuntimeError) as e:
            steps.append(f"scheduled pings not moved: {e}")
        else:
            if moved:
                steps.append(f"scheduled pings of {', '.join(moved)} → installed copy")
    return steps


def cmd_install(_args: argparse.Namespace) -> int:
    dest = install_dir()
    src = SCRIPT.parent.parent
    if src == dest.resolve():
        # This is the installed copy: install again from the folder it came from, with that folder's
        # own installer (it may be newer than this copy's).
        mark = read_json(dest / INSTALL_MARK, {}) or {}
        origin = Path(str(mark.get("source") or ""))
        if not mark.get("source") or not (origin / "bin" / "cas").is_file():
            return die("this is the installed copy and the folder it came from is gone; "
                       "run `<source folder>/bin/cas install`")
        return subprocess.call([shutil.which("python3") or sys.executable, str(origin / "bin" / "cas"), "install"])
    files = source_files(src)
    try:
        for rel in files:  # each file replaced whole, so none is ever half-written; the entry point last
            out = dest / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f".{out.name}.tmp")
            shutil.copyfile(src / rel, tmp)  # not copy2: a new mtime makes Python recompile it
            os.chmod(tmp, 0o755 if rel.parts[0] == "bin" else 0o644)
            os.replace(tmp, out)
        for old in (dest / "cas").glob("*.py"):
            if Path("cas") / old.name not in files:
                old.unlink()  # gone from the source
    except OSError as e:
        return die(f"could not install into {dest}: {e}")
    version = git_version(src)
    write_json_atomic(dest / INSTALL_MARK, {"source": str(src), "version": version, "installed_at": time.time()},
                      0o644)
    print(f"✓ installed {version or 'cas ' + __version__} from {src} into {dest}")
    for s in repoint_setup(Config.load(), (dest / "bin" / "cas").resolve()):
        print(f"· {s}")
    print("Edits to the source folder no longer change the cas you use. To update, run `cas install` again "
          "(on each machine).")
    return 0


SHELL_BEGIN = "# >>> claude-account-switcher >>>"
SHELL_END = "# <<< claude-account-switcher <<<"
SHELL_BLOCK = (
    SHELL_BEGIN + "\n"
    "# Plain `claude` starts on the account cas is using now (`cas uninstall` removes this).\n"
    'function claude { if command -v cas >/dev/null 2>&1; then command cas claude "$@"; '
    'else command claude "$@"; fi; }\n'
    + SHELL_END + "\n"
)
_SHELL_RE = re.escape(SHELL_BEGIN) + r".*?" + re.escape(SHELL_END) + r"\n?"
SHELL_BLOCK_RE = re.compile(_SHELL_RE, re.S)
SHELL_BLOCK_REMOVE_RE = re.compile(r"(?:(?<=\n)\n)?" + _SHELL_RE, re.S)  # plus the blank line we added


def shell_rc_files(existing_only: bool = False) -> List[Path]:
    rcs = [f for f in (home() / ".zshrc", home() / ".bashrc") if f.exists() or f.is_symlink()]
    if rcs or existing_only:
        return rcs
    return [home() / (".bashrc" if os.path.basename(os.environ.get("SHELL", "")) == "bash" else ".zshrc")]


def _read_rc(rc: Path) -> Optional[str]:
    try:
        return rc.read_text(encoding="utf-8") if rc.exists() else ""
    except (OSError, UnicodeDecodeError):
        return None


def shell_hook_rc(shell: Optional[str] = None) -> Optional[Path]:
    """The rc file that holds the `claude` function: the one `shell` reads, if it has it."""
    have = [rc for rc in shell_rc_files(existing_only=True) if SHELL_BEGIN in (_read_rc(rc) or "")]
    mine = ".bashrc" if os.path.basename(shell or "") == "bash" else ".zshrc"
    return next((rc for rc in have if rc.name == mine), have[0] if have else None)


def shell_hook_installed() -> bool:
    return shell_hook_rc() is not None


def install_shell_hook() -> str:
    """Make plain `claude` start on the current account: a shell function in ~/.zshrc / ~/.bashrc."""
    out = []
    for rc in shell_rc_files():
        real = Path(os.path.realpath(rc))  # keep a dotfiles symlink pointing where it did
        text = _read_rc(real)
        if text is None:
            out.append(f"skipped {rc}: unreadable")
            continue
        if SHELL_BLOCK_RE.search(text):
            new = SHELL_BLOCK_RE.sub(lambda _m: SHELL_BLOCK, text, count=1)
        else:
            new = text + ("" if not text else ("\n" if text.endswith("\n") else "\n\n")) + SHELL_BLOCK
        if new == text:
            out.append(f"{rc.name} already set")
            continue
        write_text_atomic(real, new, None if real.exists() else 0o644)
        out.append(f"added to {rc}")
    return "plain `claude` starts on the current account: " + ", ".join(out) + \
        " (takes effect in new terminals, or after `source` in this one)"


def remove_shell_hook() -> List[str]:
    out = []
    for rc in shell_rc_files(existing_only=True):
        real = Path(os.path.realpath(rc))
        text = _read_rc(real)
        if text and SHELL_BEGIN in text:
            write_text_atomic(real, SHELL_BLOCK_REMOVE_RE.sub("", text, count=1))
            out.append(f"shell function removed from {rc}")
    return out


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
    if not args.no_shell:
        steps.append(install_shell_hook())
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
    for s in remove_shell_hook():
        print(f"· {s} (plain `claude` is back on ~/.claude in new terminals)")
    link = home() / ".local" / "bin" / "cas"
    installed_script = (install_dir() / "bin" / "cas").resolve()
    if link.is_symlink() and Path(os.path.realpath(link)) in (SCRIPT, installed_script):
        link.unlink()
        print(f"· {link} removed")
    if install_dir().is_dir():
        shutil.rmtree(install_dir(), ignore_errors=True)
        print(f"· installed copy removed ({install_dir()})")
    print(f"Profiles and logins are kept in {cas_home()}; `cas remove <name> --purge` deletes one.")
    return 0


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
    dest = install_dir()
    if SCRIPT.parent.parent == dest.resolve():
        mark = read_json(dest / INSTALL_MARK, {}) or {}
        origin = Path(str(mark.get("source") or ""))
        stale = install_differs(origin, dest) if mark.get("source") and (origin / "cas").is_dir() else []
        at = when(float(mark.get("installed_at") or 0) or None)
        print(f"{style.warn('!') if stale else ok} cas: installed copy of {mark.get('version') or '?'} ({at}) "
              f"from {origin}" + (f"; the source changed since ({len(stale)} files) → cas install" if stale else ""))
    else:
        print(f"{ok} cas: running from the source folder {SCRIPT.parent.parent}" +
              ("; `cas` on PATH may be the installed copy" if dest.is_dir() else
               "; `cas install` copies it out, so edits here don't change the cas you use"))
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
    if shell_hook_installed():
        print(f"{ok} plain claude: starts on the current account, now {active_profile(cfg).name}")
    else:
        print(f"{bad} plain claude: always on ~/.claude ({cfg.hub().name}); `cas init` makes it follow the "
              "current account")
    if keychain.supported():
        locked = keychain.is_locked()
        if locked:
            print(f"{ok} Keychain: locked (usual over SSH). Logins made now go to .credentials.json; one kept in "
                  "the Keychain can't be used until it's unlocked")
        elif locked is False:
            how = keychain.autolock()
            print(f"{ok} Keychain: unlocked" + (f"; locks again {how}, and a session open then can't renew a "
                                                 "login kept there" if how else ""))
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
        login = keychain.check(p) if p.logged_in() else None
        if login is not None:
            if login.in_keychain is False and not login.in_file:
                issues.append(f"no stored login found (cas login {p.name})")
            elif login.stranded:
                issues.append("its login is in the locked Keychain: Claude Code can't read or renew it "
                              "(security unlock-keychain, or log in again)")
            elif login.stale_keychain_copy:
                issues.append("an older copy of its login is in the Keychain and takes over when the Keychain "
                              "unlocks (cas sync removes it)")
        if p.account_uuid and p.account_uuid in seen:
            issues.append(f"same account as {seen[p.account_uuid]}")
        if p.account_uuid:
            seen[p.account_uuid] = p.name
        if issues:
            problems += 1
            print(f"{bad} {label}: " + "; ".join(issues))
        else:
            print(f"{ok} {label}: {p.email} ({p.plan})" + (f", login in {login.where()}" if login else ""))
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
    s.add_argument("--no-shell", action="store_true", help="leave plain `claude` on ~/.claude (no shell function)")
    s.set_defaults(fn=cmd_init)

    s = sub.add_parser("install", help="run cas from a copy, so edits to its source don't change it (again to update)")
    s.set_defaults(fn=cmd_install)

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
    s.add_argument("--no-agents", action="store_true",
                   help="don't ask Claude to continue the agents the last session stopped")
    s.add_argument("claude_args", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_handoff)

    s = sub.add_parser("send", help="put this directory's last conversation in the shared folder, for another machine")
    s.add_argument("--session", help="conversation id or its start (default: this session, or the latest here)")
    s.set_defaults(fn=cmd_send)

    s = sub.add_parser("receive", help="continue here a conversation another machine sent (cas send)")
    s.add_argument("profile", nargs="?", help="account to run it on (default: the one cas run would pick)")
    s.add_argument("--session", help="conversation id or its start (default: the one sent for this folder)")
    s.add_argument("--list", action="store_true", help="show what's waiting in the shared folder")
    s.add_argument("--discard", action="store_true", help="remove it from the shared folder without resuming it")
    s.add_argument("-y", "--yes", action="store_true", help="later handoffs without asking")
    s.add_argument("--no-agents", action="store_true",
                   help="don't ask Claude to continue the agents the last session stopped")
    s.add_argument("claude_args", nargs=argparse.REMAINDER)
    s.set_defaults(fn=cmd_receive)

    s = sub.add_parser("reset", help="use a limit reset after showing what's left, or record the ones you have")
    s.add_argument("profile", nargs="?")
    s.add_argument("--kind", choices=["session", "weekly", "full"], help="reset kind (with --set, default full)")
    s.add_argument("--set", type=int, metavar="N", help="record how many resets claude.ai shows (0 clears)")
    s.add_argument("--expires", metavar="DATE", help="with --set: when the first one expires (YYYY-MM-DD [HH:MM])")
    s.add_argument("-c", "--continue", dest="cont", action="store_true", help="continue the last conversation here")
    s.set_defaults(fn=cmd_reset)

    s = sub.add_parser("pick", help="print the best profile name (for scripts)")
    s.add_argument("--exclude", action="append")
    s.set_defaults(fn=cmd_pick)

    sub.add_parser("claude", help="start Claude Code on the current account (what plain `claude` runs after init)")

    s = sub.add_parser("use", help="make an account the current one, where fresh sessions start (all machines)")
    s.add_argument("profile")
    s.set_defaults(fn=cmd_use)

    s = sub.add_parser("env", help="print shell exports to use a profile with plain `claude`")
    s.add_argument("profile", nargs="?", help="default: the current account")
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
    if argv and argv[0] == "claude":
        return cmd_claude(argv[1:])
    if argv and argv[0] == REFRESH_CMD:
        return cmd_refresh(argv[1:])
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
