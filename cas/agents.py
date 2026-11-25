"""The subagents of a conversation, from what Claude Code writes next to its transcript.

Claude Code keeps each subagent's own transcript beside the conversation's:
<project>/<session>/subagents/agent-<id>.jsonl, plus agent-<id>.meta.json, which gets
"finished": true when the agent's run ends. Agents run inside the Claude Code process, on its
login, so they stop when it does, and a handoff has to end it:
- closing the session leaves an agent without "finished" (its last message is often
  "[Request interrupted by user]");
- the usage limit ends it as well: "finished": true, with the limit error as its last message.
Its transcript is complete either way. In a session resumed with `--resume`, SendMessage to the
agent's id continues it with its context, on the new session's account (checked against
Claude Code 2.1.330). So when cas moves a conversation, it asks Claude to do that.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .usage import parse_time

LIMIT_RE = re.compile(
    r"hit your (?:[\w-]+ )?limit|usage limit reached|limit reached|rate_limit_error",
    re.I,
)
# An agent stopped this long before its conversation's last message belongs to an earlier part of
# it, which went on without it: a handoff doesn't bring it back.
HANDOFF_WINDOW_S = 3600
# In an open session, an unfinished agent that wrote nothing for this long counts as gone.
RUNNING_QUIET_S = 900
INTERRUPTED = "[Request interrupted"


def limit_error(obj: Dict[str, Any]) -> bool:
    """A transcript entry in which Claude Code reports the usage limit (not someone talking about it)."""
    if not (obj.get("isApiErrorMessage") or obj.get("type") == "system" or obj.get("error")):
        return False
    return obj.get("error") == "rate_limit" or bool(LIMIT_RE.search(json.dumps(obj)))


def _tail(path: Path, size: int) -> List[Dict[str, Any]]:
    with open(path, "rb") as fh:
        end = fh.seek(0, 2)
        fh.seek(max(0, end - size))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    if end > size:
        lines = lines[1:]  # starts mid-line
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def last_message(path: Path) -> Optional[Dict[str, Any]]:
    """The last user or assistant entry of a transcript."""
    for size in (64 * 1024, 1024 * 1024):
        try:
            entries = _tail(path, size)
            whole = path.stat().st_size <= size
        except OSError:
            return None
        for obj in reversed(entries):
            if obj.get("type") in ("user", "assistant"):
                return obj
        if whole:
            break
    return None


def _text(obj: Dict[str, Any]) -> str:
    content = (obj.get("message") or {}).get("content")
    if isinstance(content, list):
        return " ".join(str(c.get("text", "")) for c in content if isinstance(c, dict))
    return str(content or "")


def last_at(path: Path) -> float:
    """When a transcript last had a message (its file time when that can't be told)."""
    m = last_message(path)
    ts = parse_time(m.get("timestamp")) if m else None
    if ts is not None:
        return ts
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


@dataclass
class Agent:
    id: str
    description: str
    kind: str
    depth: int
    finished: bool
    transcript: Path
    _last: Any = field(default=False, repr=False)  # False = not read yet

    def last(self) -> Optional[Dict[str, Any]]:
        if self._last is False:
            self._last = last_message(self.transcript)
        return self._last

    def mtime(self) -> float:
        try:
            return self.transcript.stat().st_mtime
        except OSError:
            return 0.0

    def last_at(self) -> float:
        m = self.last()
        ts = parse_time(m.get("timestamp")) if m else None
        return ts if ts is not None else self.mtime()

    def state(self) -> str:
        """"done", "limit" (the usage limit stopped it), "interrupted" (its session closed mid-run),
        or "running" (unfinished: at work if its session is open, cut off if it isn't)."""
        m = self.last()
        if m is not None and limit_error(m):
            return "limit"
        if self.finished:
            return "done"
        if m is not None and m.get("type") == "user" and _text(m).lstrip().startswith(INTERRUPTED):
            return "interrupted"
        return "running"

    def name(self) -> str:
        return self.description or self.kind or self.id


def subagents_dir(transcript: Path) -> Path:
    return transcript.with_suffix("") / "subagents"  # <project>/<session>.jsonl -> <project>/<session>/


def session_agents(transcript: Path) -> List[Agent]:
    d = subagents_dir(transcript)
    out: List[Agent] = []
    if not d.is_dir():
        return out
    for meta_path in sorted(d.glob("agent-*.meta.json")):
        aid = meta_path.name[len("agent-"):-len(".meta.json")]
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(meta, dict):
            continue
        try:
            depth = int(meta.get("spawnDepth") or 1)
        except (TypeError, ValueError):
            depth = 1
        out.append(Agent(aid, str(meta.get("description") or ""), str(meta.get("agentType") or ""), depth,
                         meta.get("finished") is True, d / f"agent-{aid}.jsonl"))
    return out


def running(agents: Sequence[Agent], now: float) -> List[Agent]:
    """In an open session: the agents at work."""
    return [a for a in agents if a.state() == "running" and now - a.mtime() < RUNNING_QUIET_S]


def cut_off(agents: Sequence[Agent], since: float) -> List[Agent]:
    """In a closed session: the agents it stopped since `since`, which Claude can continue.

    Only the ones the conversation launched itself (an agent's own agents are its business)."""
    return [a for a in agents
            if a.depth <= 1 and a.state() in ("running", "limit", "interrupted") and a.last_at() >= since]


def resume_prompt(agents: Sequence[Agent], account: Optional[str], machine: Optional[str] = None) -> str:
    """The first message of the resumed session: continue the agents the last one stopped.

    machine: the conversation came from that machine (`cas receive`), on whichever account runs it here."""
    moved = (f"This conversation moved to this machine from {machine}." if machine
             else f"This conversation now runs on the {account} account.")
    lines = [f"(cas) {moved} When the previous session ended, these agents stopped before they finished:"]
    for a in agents:
        why = "it hit the usage limit" if a.state() == "limit" else "the session closed"
        lines.append(f"- {a.name()} (agent id {a.id}): {why}. Its transcript: {a.transcript}")
    lines.append("Continue each one with SendMessage to its agent id: tell it that it was interrupted and should "
                 "pick up where it stopped. If one can't be reached, start a new agent for its task and point it "
                 "to that transcript. Then carry on with what you were doing.")
    return "\n".join(lines)
