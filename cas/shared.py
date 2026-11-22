"""Share memory between machines through a folder they keep in sync.

The same project has a different path on each Mac (/Users/alice/code/X vs /Users/bob/code/X),
so Claude Code keeps its auto-memory under a different project key on each one. cas maps both
to one place in the shared store, keyed by the path relative to `projects_root` (default: home).

Store layout (<store> is a folder your machines keep in sync):
    user/CLAUDE.md                 user memory
    memory/<project>/...           auto-memory of each project ("code-X" for ~/code/X)
    agents/ commands/ skills/      your agents, commands and skills (not skills/synced: that one
                                   is Claude Code's own per-account sync of claude.ai skills)
    skills-links.json              top-level skills that are symlinks under projects_root

Files are copied, never symlinked into the shared folder (Claude Code refuses some writes through symlinks).
Each file is synced three-way against the version seen at the last sync, so cas knows which side
changed, and deletions propagate. Edited on both machines: MEMORY.md indexes are merged line by
line; for other files the newer one wins and the older is kept as <name>.conflict-<host>.<ext>
(linked from MEMORY.md in memory folders). Nothing is dropped.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Set, Tuple

from .config import cas_home, copy_atomic, home, hub_dir, read_json, write_json_atomic, write_text_atomic

SKIP_NAMES = {".DS_Store", "Icon\r", ".dropbox", ".dropbox.attr"}  # Finder and sync-client metadata
SKIP_DIRS = {"__pycache__", "node_modules", ".git", ".venv"}
SKILLS_EXCLUDE = {"synced"}  # managed by Claude Code per account


def host() -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "-", socket.gethostname().split(".")[0]) or "host"


def sanitize(path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]", "-", path)


def base_root() -> Path:
    return cas_home() / "shared-base"


# --------------------------------------------------------------------------- tree sync

def _sha(p: Path) -> Optional[str]:
    try:
        if p.is_symlink() or not p.is_file():
            return None
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except OSError:
        return None


def _files(root: Path, exclude_top: Set[str] = frozenset()) -> Set[Path]:
    out: Set[Path] = set()
    if not root.is_dir():
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        rel_dir = Path(dirpath).relative_to(root)
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not (Path(dirpath) / d).is_symlink()
                       and not (rel_dir == Path(".") and d in exclude_top)]
        for f in filenames:
            p = Path(dirpath) / f
            if f in SKIP_NAMES or f.endswith(".cas-tmp") or p.is_symlink():
                continue
            if rel_dir == Path(".") and f in exclude_top:
                continue
            out.add(p.relative_to(root))
    return out


def _prune(p: Path, stop: Path) -> None:
    d = p.parent
    while d != stop and stop in d.parents:
        try:
            d.rmdir()
        except OSError:
            return
        d = d.parent


def _put(src: Optional[Path], dst: Path, root: Path) -> None:
    """Make dst equal to src (copy), or delete dst when src is None."""
    if src is not None and src.exists():
        copy_atomic(src, dst)
    elif dst.exists() or dst.is_symlink():
        dst.unlink()
        _prune(dst, root)


def _union_lines(primary: str, other: str) -> str:
    lines = primary.rstrip("\n").split("\n") if primary.strip() else []
    seen = {line.rstrip() for line in lines}
    for line in other.rstrip("\n").split("\n"):
        if line.rstrip() and line.rstrip() not in seen:
            lines.append(line)
            seen.add(line.rstrip())
    return "\n".join(lines) + "\n"


def _placeholders(root: Path) -> bool:
    """iCloud keeps evicted files as '.<name>.icloud': their content isn't here yet."""
    if not root.is_dir():
        return False
    return any(f.startswith(".") and f.endswith(".icloud") for _, _, fs in os.walk(root) for f in fs)


MASS_DELETE_MIN = 5  # deleting at least this many files...
MASS_DELETE_SHARE = 0.5  # ...and more than this share of an area at once looks like a lost folder


def sync_tree(local: Path, store: Path, base: Path, area: str, actions: List[str],
              memory: bool = False, exclude_top: Set[str] = frozenset()) -> None:
    if _placeholders(store):
        actions.append(f"{area}: some shared files aren't downloaded yet (iCloud); skipped this time")
        return
    store_files, base_files = _files(store, exclude_top), _files(base, exclude_top)
    rels = _files(local, exclude_top) | store_files | base_files
    gone = [r for r in base_files - store_files if _sha(local / r) == _sha(base / r)]
    protect = len(gone) >= MASS_DELETE_MIN and len(gone) > MASS_DELETE_SHARE * len(base_files)
    if protect:
        actions.append(f"{area}: {len(gone)} of {len(base_files)} files vanished from the shared folder at once; "
                       "not deleting them here (if you really deleted them on the other machine, delete them "
                       "here too)")
    pointers: List[str] = []
    for rel in sorted(rels):
        a, b, o = local / rel, store / rel, base / rel
        ha, hb, ho = _sha(a), _sha(b), _sha(o)
        if ha == hb:
            if ho != ha:
                _put(a if ha else None, o, base)
            continue
        if ha == ho and hb is None and protect:  # looks like a lost folder, not a deletion: push it back
            _put(a, b, store)
            continue
        if ha == ho:  # only the shared copy changed
            _put(b if hb else None, a, local)
            _put(b if hb else None, o, base)
            actions.append(f"{area}/{rel}: {'updated from' if hb else 'deleted, as on'} the other machine")
        elif hb == ho:  # only this machine changed
            _put(a if ha else None, b, store)
            _put(a if ha else None, o, base)
            actions.append(f"{area}/{rel}: {'shared' if ha else 'deletion shared'}")
        elif ha is None or hb is None:  # deleted on one side, changed on the other: keep the change
            keep = b if ha is None else a
            _put(keep, a, local)
            _put(keep, b, store)
            _put(keep, o, base)
            actions.append(f"{area}/{rel}: kept the edited copy (the other side had deleted it)")
        elif memory and rel.name == "MEMORY.md":
            merged = _union_lines(b.read_text(errors="replace"), a.read_text(errors="replace"))
            for dst in (a, b, o):
                write_text_atomic(dst, merged, 0o644)
            actions.append(f"{area}/{rel}: merged both machines' entries")
        else:
            newer, older, older_tag = (a, b, "shared") if a.stat().st_mtime >= b.stat().st_mtime else (b, a, host())
            conflict = rel.with_name(f"{rel.stem}.conflict-{older_tag}{rel.suffix}")
            for root in (local, store, base):
                copy_atomic(older, root / conflict)
            for dst in (a, b, o):
                if dst != newer:
                    copy_atomic(newer, dst)
            actions.append(f"{area}/{rel}: edited on both machines; kept the newer, the other is {conflict.name}")
            if memory:
                pointers.append(f"- [{conflict.stem} (older version, from a sync conflict)]({conflict.as_posix()})")
    if memory and pointers:
        for root in (local, store, base):
            idx = root / "MEMORY.md"
            text = idx.read_text(errors="replace") if idx.exists() else ""
            write_text_atomic(idx, _union_lines(text, "\n".join(pointers)), 0o644)


def sync_file(local: Path, store: Path, base: Path, area: str, actions: List[str]) -> None:
    """One text file, same rules as sync_tree; edits on both sides are merged line by line."""
    a, b, o = local, store, base
    ha, hb, ho = _sha(a), _sha(b), _sha(o)
    if ha == hb:
        if ho != ha:
            _put(a if ha else None, o, o.parent)
        return
    if ha == ho:
        _put(b if hb else None, a, a.parent)
        _put(b if hb else None, o, o.parent)
        actions.append(f"{area}: updated from the other machine")
    elif hb == ho:
        _put(a if ha else None, b, b.parent)
        _put(a if ha else None, o, o.parent)
        actions.append(f"{area}: shared")
    elif ha is None or hb is None:
        keep = b if ha is None else a
        for dst in (a, b, o):
            _put(keep, dst, dst.parent)
        actions.append(f"{area}: kept the edited copy")
    else:
        merged = _union_lines(b.read_text(errors="replace"), a.read_text(errors="replace"))
        for dst in (a, b, o):
            write_text_atomic(dst, merged, 0o644)
        actions.append(f"{area}: edited on both machines; merged line by line")


# --------------------------------------------------------------------------- areas

def projects_root(settings: Dict) -> Path:
    return Path(os.path.expanduser(settings.get("projects_root") or "~"))


def store_path(settings: Dict) -> Optional[Path]:
    s = settings.get("shared_store")
    return Path(os.path.expanduser(s)) if s else None


def project_memory_pairs(settings: Dict, store: Path, only_key: Optional[str] = None) -> List[Tuple[Path, Path, str]]:
    """(local memory dir, store dir, label) for every project under projects_root known on either side.

    only_key: limit to one local project key (the folder name under ~/.claude/projects).
    """
    prefix = sanitize(str(projects_root(settings)))
    projects = hub_dir() / "projects"
    names: Set[str] = set()
    if projects.is_dir():
        for d in projects.iterdir():
            if d.name.startswith(prefix + "-") and (d / "memory").is_dir():
                names.add(d.name[len(prefix) + 1:])
    mem_store = store / "memory"
    if mem_store.is_dir():
        names |= {d.name for d in mem_store.iterdir() if d.is_dir()}
    if only_key is not None:
        names = {n for n in names if f"{prefix}-{n}" == only_key}
        if only_key.startswith(prefix + "-"):
            names.add(only_key[len(prefix) + 1:])
    return [(projects / f"{prefix}-{n}" / "memory", mem_store / n, f"memory/{n}") for n in sorted(names)]


def sync_skill_links(store: Path, settings: Dict, actions: List[str]) -> None:
    root = projects_root(settings)
    root_real = Path(os.path.realpath(root))
    manifest_path = store / "skills-links.json"
    manifest = read_json(manifest_path, {}) or {}
    skills = hub_dir() / "skills"
    changed = False
    if skills.is_dir():
        for e in skills.iterdir():
            if e.is_symlink():
                target = Path(os.path.realpath(e))
                if root_real in target.parents and manifest.get(e.name) != str(target.relative_to(root_real)):
                    manifest[e.name] = str(target.relative_to(root_real))
                    changed = True
    for name, rel in manifest.items():
        link = skills / name
        target = root / rel
        if not link.exists() and not link.is_symlink() and target.exists():
            skills.mkdir(parents=True, exist_ok=True)
            os.symlink(str(target), str(link))
            actions.append(f"skills/{name}: linked to {target}")
    if changed:
        write_json_atomic(manifest_path, manifest, 0o644)


STORE_MARKER = "store.json"


def check_store_identity(store: Path, actions: List[str]) -> None:
    """Forget the sync base when the shared folder is new, emptied or replaced.

    The base says what both sides looked like at the last sync. Compared against a different or
    empty folder, every missing file would look deleted on the other machine and be deleted here.
    Without a base, the next sync is a first sync again: nothing is deleted, everything re-shared.
    """
    base = base_root()
    marker = read_json(store / STORE_MARKER, None)
    store_id = marker.get("id") if isinstance(marker, dict) else None
    known_id = read_json(base / ".store.json", {}) or {}
    same_path = known_id.get("path") in (None, str(store))
    store_has_files = store.is_dir() and any(p.name != STORE_MARKER for p in store.iterdir())
    reset = False
    if not same_path:
        reset, why = True, "the shared folder changed"
    elif store_id is None and not store_has_files and base.exists() and any(base.iterdir()):
        reset, why = True, "the shared folder is empty"
    elif store_id is not None and known_id.get("id") not in (None, store_id):
        reset, why = True, "the shared folder was replaced"
    if reset:
        shutil.rmtree(base, ignore_errors=True)
        actions.append(f"{why}: starting a fresh sync (nothing is deleted, this machine's files are re-shared)")
    if store_id is None:
        import uuid

        store_id = uuid.uuid4().hex
        write_json_atomic(store / STORE_MARKER, {"id": store_id, "created_by": host()}, 0o644)
    base.mkdir(parents=True, exist_ok=True)
    write_json_atomic(base / ".store.json", {"id": store_id, "path": str(store)}, 0o600)


@contextmanager
def _lock(wait: bool) -> Iterator[bool]:
    path = cas_home() / "shared.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | (0 if wait else fcntl.LOCK_NB))
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(fh, fcntl.LOCK_UN)


def sync(settings: Dict, quick: bool = False, only_key: Optional[str] = None, wait: bool = True) -> List[str]:
    """Sync this machine with the shared store. quick = user memory + project memory only."""
    store = store_path(settings)
    if store is None:
        return []
    actions: List[str] = []
    with _lock(wait) as got:
        if not got:
            return ["another sync is running; skipped"]
        store.mkdir(parents=True, exist_ok=True)
        check_store_identity(store, actions)
        base = base_root()
        sync_file(hub_dir() / "CLAUDE.md", store / "user" / "CLAUDE.md", base / "user" / "CLAUDE.md",
                  "CLAUDE.md", actions)
        for local, shared, label in project_memory_pairs(settings, store, only_key):
            sync_tree(local, shared, base / label, label, actions, memory=True)
        if not quick:
            for area in ("agents", "commands"):
                sync_tree(hub_dir() / area, store / area, base / area, area, actions)
            sync_tree(hub_dir() / "skills", store / "skills", base / "skills", "skills", actions,
                      exclude_top=SKILLS_EXCLUDE)
            sync_skill_links(store, settings, actions)
        write_json_atomic(store / "machines" / f"{host()}.json",
                          {"host": host(), "home": str(home()), "synced_at": time.time()}, 0o644)
    return actions


def backup(settings: Dict) -> Path:
    """tar.gz of everything sharing touches on this machine, taken before the first share."""
    dst = cas_home() / "backups" / f"before-share-{host()}-{time.strftime('%Y%m%d-%H%M%S')}.tar.gz"
    dst.parent.mkdir(parents=True, exist_ok=True)
    hub = hub_dir()
    with tarfile.open(dst, "w:gz") as tar:
        for name in ("CLAUDE.md", "agents", "commands", "skills"):
            p = hub / name
            if p.exists():
                tar.add(str(p), arcname=name,
                        filter=lambda ti: None if "/synced" in ti.name or ti.name.endswith("/synced") else ti)
        projects = hub / "projects"
        if projects.is_dir():
            for d in projects.iterdir():
                if (d / "memory").is_dir():
                    tar.add(str(d / "memory"), arcname=f"projects/{d.name}/memory")
    return dst


def machines(store: Path) -> List[Dict]:
    d = store / "machines"
    return [m for m in (read_json(p, None) for p in sorted(d.glob("*.json"))) if isinstance(m, dict)] if d.is_dir() else []
