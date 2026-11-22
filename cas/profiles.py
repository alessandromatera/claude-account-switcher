"""Profile directories and the memory they share with the hub (~/.claude).

Directories are shared by symlink: profile/<dir> -> ~/.claude/<dir>. That covers conversation
transcripts and auto-memory (projects/), so a conversation started on one account can be
resumed on another, plus skills, agents, commands, plugins, todos, plans and file history.

Single files are synced instead of symlinked, because Claude Code refuses to write settings
through a symlink. Sync is three-way: cas keeps the last synced version as a base, so it knows
which side changed. For JSON files a key-level merge resolves edits made on both sides; for
text the newer file wins and the other is backed up.

Per account (never shared): .claude.json (login, caches, project trust), credentials,
sessions/daemon/jobs (background agents), shell snapshots, history.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import (
    Profile,
    Unreadable,
    backups_dir,
    cas_home,
    copy_atomic,
    hub_dir,
    hub_json,
    read_json,
    read_json_strict,
    write_json_atomic,
    write_text_atomic,
)

SHARED_DIRS = [
    "projects",       # transcripts (resume across accounts) + per-project auto-memory
    "agents",
    "commands",
    "skills",
    "skill",
    "plugins",
    "output-styles",
    "hooks",
    "file-history",   # checkpoints, so /rewind works after resuming elsewhere
    "todos",
    "plans",
    "agent-memory",
]
ESSENTIAL_DIRS = {"projects", "agents", "commands", "skills"}  # created in the hub if missing
SYNCED_FILES = ["settings.json", "CLAUDE.md", "keybindings.json"]
JSON_FILES = {"settings.json", "keybindings.json"}

# Copied once from ~/.claude.json into a new profile so it skips onboarding.
SEED_KEYS = ["theme", "installMethod", "autoUpdates", "lastOnboardingVersion", "editorMode"]


def _sha(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except FileNotFoundError:
        return None


def _base_path(profile: Profile, name: str) -> Path:
    return cas_home() / "sync-base" / profile.name / name


def _backup(path: Path, tag: str) -> Path:
    dst = backups_dir() / f"{path.name}.{tag}.{time.strftime('%Y%m%d-%H%M%S')}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dst)
    return dst


# --------------------------------------------------------------------------- links

def _merge_dir_into(src: Path, dst: Path, conflicts: Path, actions: List[str]) -> None:
    """Move src's contents into dst without overwriting anything in dst."""
    for child in list(src.iterdir()):
        target = dst / child.name
        if not target.exists() and not target.is_symlink():
            shutil.move(str(child), str(target))
        elif child.is_dir() and not child.is_symlink() and target.is_dir():
            _merge_dir_into(child, target, conflicts / child.name, actions)
        else:
            conflicts.mkdir(parents=True, exist_ok=True)
            shutil.move(str(child), str(conflicts / child.name))
            actions.append(f"kept hub copy of {target}; profile copy moved to {conflicts / child.name}")
    try:
        src.rmdir()
    except OSError:
        pass


def ensure_links(profile: Profile) -> List[str]:
    """Make every shared directory of `profile` a symlink into the hub. Returns what changed."""
    if profile.is_hub:
        return []
    actions: List[str] = []
    hub = hub_dir()
    profile.dir.mkdir(parents=True, exist_ok=True)
    for item in SHARED_DIRS:
        src, dst = hub / item, profile.dir / item
        if dst.is_symlink():
            if os.path.realpath(dst) == os.path.realpath(src) and src.is_dir():
                continue
            dst.unlink()
            actions.append(f"relinked {item}")
        elif dst.is_dir():
            if not src.exists():
                shutil.move(str(dst), str(src))
                actions.append(f"moved {item}/ into the hub")
            else:
                _merge_dir_into(dst, src, profile.dir / ".cas-conflicts" / item, actions)
                if dst.exists():
                    shutil.rmtree(dst)  # only empty dirs can remain after the merge
                actions.append(f"merged {item}/ into the hub")
        elif dst.exists():
            conflicts = profile.dir / ".cas-conflicts"
            conflicts.mkdir(parents=True, exist_ok=True)
            shutil.move(str(dst), str(conflicts / item))
            actions.append(f"moved stray file {item} to {conflicts}")
        if not src.exists():
            if item not in ESSENTIAL_DIRS:
                continue
            src.mkdir(parents=True, exist_ok=True)
        os.symlink(str(src), str(dst))
    return actions


# --------------------------------------------------------------------------- file sync

def _merge_json(base: Any, hub: Any, prof: Any) -> Any:
    """Three-way merge of JSON objects, key by key; other types: prefer whichever changed."""
    if isinstance(base, dict) and isinstance(hub, dict) and isinstance(prof, dict):
        out: Dict[str, Any] = {}
        for k in list(hub.keys()) + [k for k in prof.keys() if k not in hub]:
            b, h, p = base.get(k, _MISSING), hub.get(k, _MISSING), prof.get(k, _MISSING)
            if h == p:
                v = h
            elif h == b:
                v = p
            elif p == b:
                v = h
            else:
                v = _merge_json(b if b is not _MISSING else {}, h, p) if isinstance(h, dict) and isinstance(p, dict) else p
            if v is not _MISSING:
                out[k] = v
        return out
    if hub == base:
        return prof
    return hub if prof == base else prof


class _Missing:
    def __repr__(self) -> str:
        return "<missing>"


_MISSING = _Missing()


def sync_file(profile: Profile, name: str) -> Optional[str]:
    """Bring `name` in sync between hub and profile. Returns a description if anything changed."""
    hub_f, prof_f, base_f = hub_dir() / name, profile.dir / name, _base_path(profile, name)
    if prof_f.is_symlink():  # older layouts symlinked files; Claude Code can't write through them
        prof_f.unlink()
    h, p, b = _sha(hub_f), _sha(prof_f), _sha(base_f)

    def remember(src: Path) -> None:
        copy_atomic(src, base_f)

    if h is None and p is None:
        return None
    if h == p:
        if b != h:
            remember(hub_f)
        return None
    if p is None:
        copy_atomic(hub_f, prof_f)
        remember(hub_f)
        return f"{name}: hub -> {profile.name}"
    if h is None:
        copy_atomic(prof_f, hub_f)
        remember(prof_f)
        return f"{name}: {profile.name} -> hub"
    if b == h:  # only the profile changed
        copy_atomic(prof_f, hub_f)
        remember(prof_f)
        return f"{name}: {profile.name} -> hub"
    if b == p:  # only the hub changed
        copy_atomic(hub_f, prof_f)
        remember(hub_f)
        return f"{name}: hub -> {profile.name}"

    # Both changed (or no base yet).
    if name in JSON_FILES:
        try:
            hub_d, prof_d = read_json_strict(hub_f), read_json_strict(prof_f)
            base_d = read_json(base_f, {}) if b else {}
        except Unreadable:
            hub_d = prof_d = None
        if isinstance(hub_d, dict) and isinstance(prof_d, dict):
            merged = _merge_json(base_d if isinstance(base_d, dict) else {}, hub_d, prof_d)
            text = json.dumps(merged, indent=2, ensure_ascii=False) + "\n"
            write_text_atomic(hub_f, text)
            copy_atomic(hub_f, prof_f)
            remember(hub_f)
            return f"{name}: merged edits from hub and {profile.name}"
    newer_is_prof = prof_f.stat().st_mtime > hub_f.stat().st_mtime
    winner, loser = (prof_f, hub_f) if newer_is_prof else (hub_f, prof_f)
    saved = _backup(loser, "hub" if loser == hub_f else profile.name)
    copy_atomic(winner, loser)
    remember(winner)
    return f"{name}: both sides changed; kept the newer one, backup of the other in {saved}"


def sync_files(profile: Profile) -> List[str]:
    if profile.is_hub:
        return []
    out = []
    for name in SYNCED_FILES:
        r = sync_file(profile, name)
        if r:
            out.append(r)
    return out


# --------------------------------------------------------------------------- .claude.json

def sync_mcp(profile: Profile) -> Optional[str]:
    """Copy user-scope MCP servers from ~/.claude.json into the profile (one way)."""
    if profile.is_hub:
        return None
    hub_servers = (read_json(hub_json(), {}) or {}).get("mcpServers") or {}
    if not isinstance(hub_servers, dict) or not hub_servers:
        return None
    try:
        data = read_json_strict(profile.global_json)
    except Unreadable:
        return None  # never rewrite a config we could not parse: it holds the login
    if data is None or not isinstance(data, dict):
        return None  # profile not initialised yet; seed_profile_json handles it
    cur = data.get("mcpServers") or {}
    merged = dict(cur)
    merged.update(hub_servers)
    if merged == cur:
        return None
    data["mcpServers"] = merged
    write_json_atomic(profile.global_json, data, 0o600)
    added = sorted(set(hub_servers) - set(cur))
    return f"MCP servers synced to {profile.name}" + (f" (+{', '.join(added)})" if added else "")


def seed_profile_json(profile: Profile) -> bool:
    """Create the profile's .claude.json so it skips onboarding and trusts the same projects."""
    if profile.is_hub or profile.global_json.exists():
        return False
    hub = read_json(hub_json(), {}) or {}
    data: Dict[str, Any] = {"hasCompletedOnboarding": True}
    for k in SEED_KEYS:
        if k in hub:
            data[k] = hub[k]
    if isinstance(hub.get("mcpServers"), dict):
        data["mcpServers"] = hub["mcpServers"]
    trusted = {
        path: {"hasTrustDialogAccepted": True}
        for path, cfg in (hub.get("projects") or {}).items()
        if isinstance(cfg, dict) and cfg.get("hasTrustDialogAccepted")
    }
    if trusted:
        data["projects"] = trusted
    write_json_atomic(profile.global_json, data, 0o600)
    return True


# --------------------------------------------------------------------------- lifecycle

def create_dir(profile: Profile) -> List[str]:
    assert profile.config_dir is not None
    profile.config_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(profile.config_dir, 0o700)
    actions = []
    if seed_profile_json(profile):
        actions.append("seeded .claude.json")
    actions += ensure_links(profile)
    actions += sync_files(profile)
    return actions


def prepare(target: Profile, all_profiles: List[Profile], sync_mcp_servers: bool = True) -> List[str]:
    """Before launching Claude on `target`: fix links, sync files for everyone, push MCP servers."""
    actions: List[str] = []
    for p in all_profiles:
        if p.is_hub or not p.dir.exists():
            continue
        try:
            actions += sync_files(p)
        except OSError as e:
            actions.append(f"could not sync {p.name}: {e}")
    if not target.is_hub:
        actions += ensure_links(target)
        if sync_mcp_servers:
            r = sync_mcp(target)
            if r:
                actions.append(r)
    return actions


def link_problems(profile: Profile) -> List[str]:
    if profile.is_hub:
        return []
    out = []
    for item in SHARED_DIRS:
        src, dst = hub_dir() / item, profile.dir / item
        if not src.exists():
            if item in ESSENTIAL_DIRS:
                out.append(f"hub has no {item}/")
            continue
        if not dst.is_symlink() or os.path.realpath(dst) != os.path.realpath(src):
            out.append(f"{item}/ is not shared")
    for name in SYNCED_FILES:
        if (hub_dir() / name).exists() and _sha(hub_dir() / name) != _sha(profile.dir / name):
            out.append(f"{name} out of sync")
    return out
