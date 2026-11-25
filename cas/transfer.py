"""Move a conversation to another machine through the shared folder (`cas send`, `cas receive`).

`cas share` leaves conversations on their machine. `cas send` puts one in the shared folder;
`cas receive` on the other machine puts it where Claude Code looks for it there, so
`claude --resume <id>` continues it.

Shared folder layout:
    handoffs/<session>.tar.gz   the conversation: its transcript, its folder (subagents, tool
                                results), its file checkpoints (for /rewind) and its todos
    handoffs/<session>.json     written after the archive: who sent it, from which folder (also
                                relative to projects_root), when, and the archive's sha256, so the
                                other machine can tell an archive still syncing from a whole one

The project can have a different path on each machine (/Users/alice/code/X vs /Users/bob/code/X).
As with project memory, cas looks for it at the same place relative to projects_root, and rewrites
the sender's paths in the conversation (project folder and home) so Claude finds the files here.
Claude Code keys conversations by the folder's resolved path (symlinks followed), checked against
2.1.330. The receiver deletes both files once the conversation is in place.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import backups_dir, cas_home, copy_atomic, home, hub_dir, read_json, write_json_atomic

SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{7,63}$")
TOPS = ("conversation.jsonl", "conversation", "file-history", "todos")  # what an archive may hold
REWRITE_SUFFIXES = (".jsonl", ".json")  # in conversation*/ and todos/: files that hold paths


def sanitize(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def handoffs_dir(settings: Dict[str, Any]) -> Optional[Path]:
    from . import shared

    store = shared.store_path(settings)
    return store / "handoffs" if store else None


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# --------------------------------------------------------------------------- reading a transcript

def _entries(path: Path, tail: Optional[int] = None) -> List[Dict[str, Any]]:
    with open(path, "rb") as fh:
        if tail is not None:
            end = fh.seek(0, 2)
            fh.seek(max(0, end - tail))
        lines = fh.read().decode("utf-8", "replace").splitlines()
    if tail is not None and len(lines) > 1:
        lines = lines[1:]  # may start mid-line
    out = []
    for line in lines:
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def transcript_cwd(path: Path) -> Optional[str]:
    """The folder the conversation last ran in, as Claude Code recorded it."""
    for tail in (256 * 1024, None):
        entries = _entries(path, tail)
        for obj in reversed(entries):
            if isinstance(obj.get("cwd"), str) and obj["cwd"].startswith("/"):
                return obj["cwd"]
        if tail is None or path.stat().st_size <= tail:
            break
    return None


def title(path: Path) -> Optional[str]:
    """The title Claude Code gave the conversation, if it gave one."""
    found = None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"ai-title"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except ValueError:
                    continue
                if isinstance(obj, dict) and obj.get("type") == "ai-title" and obj.get("aiTitle"):
                    found = str(obj["aiTitle"])
    except OSError:
        return None
    return " ".join(found.split()) if found else None


# --------------------------------------------------------------------------- where the project is

def _root(settings: Dict[str, Any]) -> Path:
    from . import shared

    return shared.projects_root(settings)


def project_rel(settings: Dict[str, Any], cwd: str) -> Optional[str]:
    """cwd relative to projects_root ("." for the root itself), or None when it's outside."""
    root = _root(settings)
    for r in (Path(os.path.realpath(root)), root):
        try:
            return Path(cwd).relative_to(r).as_posix()
        except ValueError:
            continue
    return None


def _safe_rel(rel: Any) -> bool:
    return isinstance(rel, str) and not rel.startswith("/") and ".." not in PurePosixPath(rel).parts


@dataclass
class Package:
    manifest: Dict[str, Any]
    path: Path  # handoffs/<session>.json

    @property
    def session(self) -> str:
        return str(self.manifest.get("session"))

    @property
    def archive(self) -> Path:
        return self.path.with_name(f"{self.session}.tar.gz")

    @property
    def host(self) -> str:
        return str(self.manifest.get("host") or "?")

    @property
    def sent_at(self) -> float:
        try:
            return float(self.manifest.get("sent_at") or 0)
        except (TypeError, ValueError):
            return 0.0

    @property
    def cwd(self) -> str:
        return str(self.manifest.get("cwd") or "")

    @property
    def label(self) -> str:
        return str(self.manifest.get("title") or "(no prompt)")

    def local_dir(self, settings: Dict[str, Any]) -> Path:
        """Where this machine keeps the project: same place relative to projects_root, else the same path."""
        rel = self.manifest.get("project")
        if rel is not None and _safe_rel(rel):
            root = _root(settings)
            return root if rel == "." else root / rel
        return Path(self.cwd)

    def problem(self) -> Optional[str]:
        """Why it can't be received yet (None when it can)."""
        a = self.archive
        if not a.exists():
            if a.with_name(f".{a.name}.icloud").exists():
                return "its archive isn't downloaded yet (iCloud); open the shared folder in Finder, or wait"
            return "its archive hasn't arrived yet; try again once the shared folder has synced"
        try:
            size, sha = a.stat().st_size, _sha(a)
        except OSError as e:
            return f"its archive can't be read: {e}"
        if sha != self.manifest.get("sha256"):
            want = self.manifest.get("size")
            got = f" ({size} of {want} bytes so far)" if isinstance(want, int) and size < want else ""
            return f"its archive is still arriving{got}; try again once the shared folder has synced"
        return None


def pending(settings: Dict[str, Any], own: bool = False) -> List[Package]:
    """Conversations waiting in the shared folder, newest first: sent from other machines (own=True: from this one)."""
    from .shared import host

    d = handoffs_dir(settings)
    out: List[Package] = []
    if d is None or not d.is_dir():
        return out
    me = host()
    for f in d.glob("*.json"):
        m = read_json(f, None)
        if not isinstance(m, dict) or not SESSION_RE.match(str(m.get("session") or "")) \
                or f.stem != m.get("session"):
            continue
        if (m.get("host") == me) == own:
            out.append(Package(m, f))
    return sorted(out, key=lambda p: p.sent_at, reverse=True)


def waiting_here(settings: Dict[str, Any], cwd: str) -> List[Package]:
    """Conversations other machines sent for the project in `cwd`."""
    here = os.path.realpath(cwd)
    return [p for p in pending(settings) if os.path.realpath(str(p.local_dir(settings))) == here]


# --------------------------------------------------------------------------- sending

def _filter(ti: tarfile.TarInfo) -> Optional[tarfile.TarInfo]:
    if not (ti.isfile() or ti.isdir()):
        return None  # no links or devices
    ti.uid = ti.gid = 0
    ti.uname = ti.gname = ""
    return ti


def pack(settings: Dict[str, Any], session: str, transcript: Path, *, label: str, still_open: bool,
         profile: Optional[str] = None, account: Optional[str] = None,
         now: Optional[float] = None) -> Package:
    """Put a conversation in the shared folder for the other machines. Replaces an earlier send of it."""
    from .shared import host

    if not SESSION_RE.match(session):
        raise ValueError(f"unexpected conversation id {session!r}")
    d = handoffs_dir(settings)
    if d is None:
        raise ValueError("no shared folder (cas share)")
    cwd = transcript_cwd(transcript)
    if not cwd:
        raise ValueError("its transcript doesn't say which folder it ran in")
    now = time.time() if now is None else now
    data = transcript.read_bytes()
    if data and not data.endswith(b"\n"):
        data = data[:data.rfind(b"\n") + 1]  # an open session may be writing its last line
    d.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{session}.", suffix=".cas-tmp", dir=str(d))
    os.close(fd)
    final = d / f"{session}.tar.gz"
    try:
        with tarfile.open(tmp, "w:gz", compresslevel=6) as tar:
            ti = tarfile.TarInfo("conversation.jsonl")
            ti.size, ti.mtime, ti.mode = len(data), int(transcript.stat().st_mtime), 0o600
            tar.addfile(ti, io.BytesIO(data))
            folder = transcript.with_suffix("")
            if folder.is_dir() and not folder.is_symlink():
                tar.add(str(folder), arcname="conversation", filter=_filter)
            fh = hub_dir() / "file-history" / session
            if fh.is_dir():
                tar.add(str(fh), arcname="file-history", filter=_filter)
            todos = hub_dir() / "todos"
            if todos.is_dir():
                for t in sorted(todos.glob(f"{session}-*.json")):
                    tar.add(str(t), arcname=f"todos/{t.name}", filter=_filter)
        os.replace(tmp, final)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    manifest = {
        "session": session,
        "host": host(),
        "home": str(home()),
        "cwd": cwd,
        "project": project_rel(settings, cwd),
        "title": label,
        "sent_at": now,
        "open": still_open,
        "profile": profile,
        "account": account,
        "size": final.stat().st_size,
        "sha256": _sha(final),
    }
    write_json_atomic(d / f"{session}.json", manifest, 0o644)
    return Package(manifest, d / f"{session}.json")


def discard(pkg: Package) -> None:
    for f in (pkg.path, pkg.archive):
        try:
            f.unlink()
        except FileNotFoundError:
            pass


# --------------------------------------------------------------------------- receiving

def rewriter(pairs: List[Tuple[str, str]]) -> Optional[Callable[[str], str]]:
    """A function that replaces whole paths in JSON text, in one pass: /Users/a/X and /Users/a/X/f, not
    /Users/a/X-old or /Volumes/v/Users/a/X. None when there is nothing to replace."""
    table: Dict[str, str] = {}
    for src, dst in pairs:
        if src and src != dst:
            table.setdefault(src, dst)
            table.setdefault(json.dumps(src, ensure_ascii=False)[1:-1], json.dumps(dst, ensure_ascii=False)[1:-1])
    if not table:
        return None
    alts = "|".join(re.escape(k) for k in sorted(table, key=len, reverse=True))
    rx = re.compile(r"(?<![\w.-])(?:" + alts + r")(?![\w.-])")
    return lambda text: rx.sub(lambda m: table[m.group(0)], text)


def _members(tar: tarfile.TarFile) -> List[tarfile.TarInfo]:
    """The archive's files, refusing anything that would land outside the places it may write to."""
    out = []
    for m in tar.getmembers():
        parts = PurePosixPath(m.name).parts
        if m.name.startswith("/") or ".." in parts or not parts or parts[0] not in TOPS:
            raise ValueError(f"unexpected entry in the archive: {m.name!r}")
        if m.isfile():
            out.append(m)
        elif not m.isdir():
            raise ValueError(f"unexpected entry in the archive: {m.name!r} (not a file)")
    return out


def _backup(transcript: Path, session: str) -> Path:
    dst = backups_dir() / "conversations" / f"{session}-{time.strftime('%Y%m%d-%H%M%S')}"
    dst.mkdir(parents=True, exist_ok=True)
    shutil.copy2(transcript, dst / transcript.name)
    folder = transcript.with_suffix("")
    if folder.is_dir():
        shutil.copytree(folder, dst / folder.name, symlinks=True)
    return dst


def unpack(settings: Dict[str, Any], pkg: Package, cwd: str) -> Tuple[Path, List[str]]:
    """Put a received conversation where Claude Code looks for it when run in `cwd`.

    Returns (its transcript here, what was done). Removes it from the shared folder."""
    problem = pkg.problem()
    if problem:
        raise ValueError(problem)
    session = pkg.session
    here = os.path.realpath(cwd)
    project = hub_dir() / "projects" / sanitize(here)
    dest = project / f"{session}.jsonl"
    # The project folder, its key in Claude Code's paths (…/projects/-Users-alice-code-X/memory), then home.
    pairs: List[Tuple[str, str]] = []
    if pkg.cwd.startswith("/"):
        pairs += [(pkg.cwd, here), (sanitize(pkg.cwd), sanitize(here))]
    sender_home = str(pkg.manifest.get("home") or "").rstrip("/")
    if sender_home.startswith("/"):
        pairs.append((sender_home, str(home()).rstrip("/")))
    fix = rewriter(pairs)
    actions: List[str] = []
    work = cas_home() / "tmp"
    work.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=str(work)) as tmp_s:
        tmp = Path(tmp_s)
        with tarfile.open(pkg.archive, "r:gz") as tar:
            for m in _members(tar):
                src = tar.extractfile(m)
                if src is None:
                    continue
                data = src.read()
                top = PurePosixPath(m.name).parts[0]
                if fix and top != "file-history" and m.name.endswith(REWRITE_SUFFIXES):
                    data = fix(data.decode("utf-8", "surrogateescape")).encode("utf-8", "surrogateescape")
                out = tmp / m.name
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(data)
                os.chmod(out, 0o600)
                os.utime(out, (m.mtime, m.mtime))
        incoming = tmp / "conversation.jsonl"
        if not incoming.is_file():
            raise ValueError("the archive has no transcript")
        if dest.exists():
            if dest.read_bytes() == incoming.read_bytes():
                actions.append("this machine already had this version of it")
            else:
                actions.append(f"this machine's earlier copy of it went to {_backup(dest, session)}")
        if fix:
            actions.append(f"paths moved from {pkg.cwd} to {here}")
        project.mkdir(parents=True, exist_ok=True)
        folder = tmp / "conversation"
        if folder.is_dir():
            local = dest.with_suffix("")
            if local.exists():
                shutil.rmtree(local)
            shutil.copytree(folder, local)
        for area in ("file-history", "todos"):
            src_dir = tmp / area
            if not src_dir.is_dir():
                continue
            dst_dir = hub_dir() / area / (session if area == "file-history" else "")
            for f in src_dir.rglob("*"):
                if f.is_file():
                    copy_atomic(f, dst_dir / f.relative_to(src_dir))
        copy_atomic(incoming, dest)
    discard(pkg)
    return dest, actions
