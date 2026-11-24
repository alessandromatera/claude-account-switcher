"""macOS launchd agents that send the priming pings.

One agent per profile: ~/Library/LaunchAgents/com.claude-account-switcher.prime.<name>.plist.
launchd runs a job missed while the Mac slept as soon as it wakes; `cas ping --scheduled`
then decides whether the ping is still worth sending.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Sequence

from .config import home, logs_dir

LABEL_PREFIX = "com.claude-account-switcher.prime."


def agents_dir() -> Path:
    return home() / "Library" / "LaunchAgents"


def plist_path(profile: str) -> Path:
    return agents_dir() / f"{LABEL_PREFIX}{profile}.plist"


def build_plist(profile: str, entries: Sequence[Dict[str, int]], python: str, script: str,
                path_env: str) -> Dict:
    log = str(logs_dir() / f"prime-{profile}.log")
    return {
        "Label": f"{LABEL_PREFIX}{profile}",
        "ProgramArguments": [python, script, "ping", profile, "--scheduled"],
        "StartCalendarInterval": list(entries),
        "EnvironmentVariables": {"PATH": path_env, "HOME": str(home())},
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "RunAtLoad": False,
        "ProcessType": "Background",
    }


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def install(profile: str, entries: Sequence[Dict[str, int]], python: str, script: str, path_env: str) -> Path:
    logs_dir().mkdir(parents=True, exist_ok=True)
    agents_dir().mkdir(parents=True, exist_ok=True)
    path = plist_path(profile)
    domain = f"gui/{os.getuid()}"
    if path.exists():
        _launchctl("bootout", domain, str(path))
    with open(path, "wb") as f:
        plistlib.dump(build_plist(profile, entries, python, script, path_env), f)
    r = _launchctl("bootstrap", domain, str(path))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed for {path.name}: {r.stderr.strip() or r.stdout.strip()}")
    return path


def installed() -> Dict[str, List[Dict[str, int]]]:
    out: Dict[str, List[Dict[str, int]]] = {}
    d = agents_dir()
    if not d.exists():
        return out
    for p in sorted(d.glob(f"{LABEL_PREFIX}*.plist")):
        try:
            with open(p, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        out[p.name[len(LABEL_PREFIX):-len(".plist")]] = list(data.get("StartCalendarInterval") or [])
    return out


def rename(old: str, new: str) -> bool:
    """Move an installed schedule to a renamed profile."""
    path = plist_path(old)
    if not path.exists():
        return False
    with open(path, "rb") as f:
        data = plistlib.load(f)
    args = list(data.get("ProgramArguments") or [])
    if len(args) >= 4 and args[2] == "ping":
        args[3] = new
    log = str(logs_dir() / f"prime-{new}.log")
    data.update({"Label": f"{LABEL_PREFIX}{new}", "ProgramArguments": args,
                 "StandardOutPath": log, "StandardErrorPath": log})
    domain = f"gui/{os.getuid()}"
    _launchctl("bootout", domain, str(path))
    path.unlink()
    new_path = plist_path(new)
    with open(new_path, "wb") as f:
        plistlib.dump(data, f)
    r = _launchctl("bootstrap", domain, str(new_path))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed for {new_path.name}: {r.stderr.strip() or r.stdout.strip()}")
    return True


def repoint(script: str) -> List[str]:
    """Make every installed schedule run `script` (`cas install` moved cas). Returns the profiles changed."""
    changed = []
    domain = f"gui/{os.getuid()}"
    for name in installed():
        path = plist_path(name)
        with open(path, "rb") as f:
            data = plistlib.load(f)
        args = list(data.get("ProgramArguments") or [])
        if len(args) < 2 or args[1] == script:
            continue
        args[1] = script
        data["ProgramArguments"] = args
        _launchctl("bootout", domain, str(path))
        with open(path, "wb") as f:
            plistlib.dump(data, f)
        r = _launchctl("bootstrap", domain, str(path))
        if r.returncode != 0:
            raise RuntimeError(f"launchctl bootstrap failed for {path.name}: {r.stderr.strip() or r.stdout.strip()}")
        changed.append(name)
    return changed


def remove(profile: str) -> bool:
    path = plist_path(profile)
    if not path.exists():
        return False
    _launchctl("bootout", f"gui/{os.getuid()}", str(path))
    path.unlink()
    return True


def remove_all() -> List[str]:
    return [name for name in list(installed()) if remove(name)]


def supported() -> bool:
    return sys.platform == "darwin"
