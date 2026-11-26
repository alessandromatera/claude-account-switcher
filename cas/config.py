"""Locations, the cas config file, and the Profile model.

Layout
------
~/.claude                    hub: the default Claude Code config dir. Shared state lives here.
~/.claude.json               hub's global config (login, MCP servers, project trust).
~/.claude-accounts/
    config.json              profiles + settings
    profiles/<name>/         CLAUDE_CONFIG_DIR of each extra account
    state/<name>.json        last known usage, last session, last ping, sync bases
    logs/                    scheduled ping logs

Every location can be redirected with an env var (CAS_HOME, CAS_HUB_DIR, CAS_HUB_JSON),
which is what the tests use.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shutil
import tempfile
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

HUB_PROFILE_DEFAULT_NAME = "default"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
RESERVED_NAMES = {"auto", "all", "hub"}
NAME_RULES = "lowercase letters, digits, '.', '-' or '_' (e.g. gmail.com), max 64 chars, not auto/all/hub"


def valid_name(name: str) -> bool:
    return bool(NAME_RE.match(name)) and ".." not in name and not name.endswith(".") \
        and name not in RESERVED_NAMES

DEFAULT_SETTINGS: Dict[str, Any] = {
    # An account counts as exhausted once its 5-hour window reaches this percentage.
    "switch_threshold": 90,
    # ...or once its weekly limit reaches this percentage.
    "weekly_threshold": 97,
    # An account with an open window is preferred only if it has at least this much left.
    "min_headroom": 15,
    # Query https://api.anthropic.com/api/oauth/usage with the profile's own token:
    # "auto" = only when cached data is older than live_max_age_s, "always", or "never".
    "live_usage": "auto",
    "live_max_age_s": 300,
    # When an idle account's login has expired (after ~8 h), renew it as Claude Code would, so its
    # usage stays current. Only while no Claude Code session runs on that account.
    "renew_logins": True,
    # What `cas run` does when Claude exits because the account hit its limit:
    # "ask" = offer to resume the conversation on the next account, "auto" = just do it, "off".
    "handoff": "ask",
    # When a conversation moves to another account, have Claude continue the agents its previous
    # session stopped (SendMessage to each agent's id) with the first message of the new session.
    "resume_agents": True,
    # The status line warns that agents are running once the 5-hour window is this many points from
    # switch_threshold (0 = never), so you can hand off when none is mid-task.
    "agent_warning": 15,
    # Copy user-scope MCP servers from ~/.claude.json into each profile before launch.
    "sync_mcp": True,
    # Model used for the tiny message that opens a 5-hour window.
    "prime_model": "haiku",
    # When `cas run` starts Claude on one account, open the 5-hour window of every other
    # account that has none open (background ping), so its reset comes sooner if you switch.
    "prime_others_on_start": True,
    # Command of a status line that was configured before cas took over (chained, not replaced).
    "statusline_chain": None,
    # Absolute path of the claude binary (resolved at init; launchd has a bare PATH).
    "claude_bin": None,
    # Memory shared between machines through a synced folder (`cas share`). None = off.
    "shared_store": None,
    # Projects at the same path relative to this folder on each machine share memory (cas share).
    "projects_root": "~",
    "schedule": {
        "start": "09:00",
        "end": "19:00",
        "days": "mon-fri",
        "strategy": "stagger",
        "first_reset_after_min": 120,
        "min_useful_min": 60,
        "profiles": None,
    },
}


# --------------------------------------------------------------------------- paths

def home() -> Path:
    return Path(os.environ.get("HOME") or str(Path.home()))


def cas_home() -> Path:
    v = os.environ.get("CAS_HOME")
    return Path(v).expanduser() if v else home() / ".claude-accounts"


def hub_dir() -> Path:
    v = os.environ.get("CAS_HUB_DIR")
    return Path(v).expanduser() if v else home() / ".claude"


def hub_json() -> Path:
    v = os.environ.get("CAS_HUB_JSON")
    return Path(v).expanduser() if v else home() / ".claude.json"


def config_path() -> Path:
    return cas_home() / "config.json"


def state_dir() -> Path:
    return cas_home() / "state"


def profiles_root() -> Path:
    return cas_home() / "profiles"


def logs_dir() -> Path:
    return cas_home() / "logs"


def backups_dir() -> Path:
    return cas_home() / "backups"


# --------------------------------------------------------------------------- json io

class Unreadable(Exception):
    """A JSON file exists but could not be parsed. Callers must not overwrite it blindly."""


class UnknownProfile(Exception):
    pass


def read_json(path: Path, default: Any = None) -> Any:
    """Read JSON; missing or unreadable files give `default`."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def read_json_strict(path: Path) -> Optional[Any]:
    """Read JSON; None if missing, Unreadable if present but broken."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        raise Unreadable(f"{path}: {e}") from e


def write_text_atomic(path: Path, text: str, mode: Optional[int] = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".cas-tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_json_atomic(path: Path, data: Any, mode: Optional[int] = None) -> None:
    write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode)


def copy_atomic(src: Path, dst: Path) -> None:
    """Copy src over dst atomically, keeping src's mtime (so sync sees them as equal)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{dst.name}.", suffix=".cas-tmp", dir=str(dst.parent))
    os.close(fd)
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def deep_merge(base: Dict[str, Any], over: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------- profile

def keychain_service_for(config_dir: Optional[Path]) -> str:
    """Keychain service Claude Code uses for a config dir.

    Claude Code (2.1.x) stores OAuth credentials under "Claude Code-credentials" for the
    default dir and "Claude Code-credentials-<sha256(CLAUDE_CONFIG_DIR)[:8]>" otherwise.
    """
    if config_dir is None:
        return "Claude Code-credentials"
    digest = hashlib.sha256(unicodedata.normalize("NFC", str(config_dir)).encode("utf-8")).hexdigest()
    return f"Claude Code-credentials-{digest[:8]}"


def keychain_account() -> str:
    user = os.environ.get("USER") or ""
    if not user:
        try:
            import pwd

            user = pwd.getpwuid(os.getuid()).pw_name
        except Exception:
            user = ""
    return user if re.fullmatch(r"[a-zA-Z0-9._-]+", user or "") else "claude-code-user"


@dataclass
class Profile:
    name: str
    config_dir: Optional[Path] = None  # None = the hub (~/.claude, CLAUDE_CONFIG_DIR unset)
    label: Optional[str] = None
    model: Optional[str] = None
    enabled: bool = True

    @property
    def is_hub(self) -> bool:
        return self.config_dir is None

    @property
    def dir(self) -> Path:
        return self.config_dir if self.config_dir is not None else hub_dir()

    @property
    def global_json(self) -> Path:
        return self.config_dir / ".claude.json" if self.config_dir is not None else hub_json()

    @property
    def keychain_service(self) -> str:
        return keychain_service_for(self.config_dir)

    def env(self, base: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        env = dict(os.environ if base is None else base)
        env.pop("CLAUDE_CONFIG_DIR", None)
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        if self.config_dir is not None:
            env["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        env["CAS_PROFILE"] = self.name
        return env

    def oauth_account(self) -> Dict[str, Any]:
        data = read_json(self.global_json, {}) or {}
        acct = data.get("oauthAccount") if isinstance(data, dict) else None
        return acct if isinstance(acct, dict) else {}

    @property
    def email(self) -> Optional[str]:
        return self.oauth_account().get("emailAddress")

    @property
    def account_uuid(self) -> Optional[str]:
        return self.oauth_account().get("accountUuid")

    @property
    def plan(self) -> str:
        acct = self.oauth_account()
        org = acct.get("organizationType") or ""
        if org.startswith("claude_"):
            return org[len("claude_"):]
        return org or ("api" if acct.get("billingType") == "api" else "-")

    def logged_in(self) -> bool:
        return bool(self.account_uuid)

    def display(self) -> str:
        return self.label or self.email or "(not logged in)"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "config_dir": str(self.config_dir) if self.config_dir is not None else None,
            "label": self.label,
            "model": self.model,
            "enabled": self.enabled,
        }

    @classmethod
    def from_dict(cls, name: str, d: Dict[str, Any]) -> "Profile":
        cd = d.get("config_dir")
        return cls(
            name=name,
            config_dir=Path(cd) if cd else None,
            label=d.get("label"),
            model=d.get("model"),
            enabled=d.get("enabled", True),
        )


def normalize_dir(p: str) -> str:
    return unicodedata.normalize("NFC", os.path.normpath(os.path.expanduser(p)))


# --------------------------------------------------------------------------- config

class Config:
    def __init__(self, data: Dict[str, Any]):
        self.data = data
        self.data.setdefault("version", 1)
        self.data.setdefault("profiles", {})
        self.data["settings"] = deep_merge(DEFAULT_SETTINGS, self.data.get("settings") or {})
        if not any(not p.get("config_dir") for p in self.data["profiles"].values()):
            # Always know about the hub profile, even before `cas init`.
            self.data["profiles"] = {HUB_PROFILE_DEFAULT_NAME: {"config_dir": None}, **self.data["profiles"]}

    @classmethod
    def load(cls) -> "Config":
        data = read_json(config_path(), None)
        return cls(data if isinstance(data, dict) else {})

    def exists(self) -> bool:
        return config_path().exists()

    def save(self) -> None:
        write_json_atomic(config_path(), self.data, 0o600)

    @property
    def settings(self) -> Dict[str, Any]:
        return self.data["settings"]

    @property
    def profiles(self) -> Dict[str, Profile]:
        return {n: Profile.from_dict(n, d) for n, d in self.data["profiles"].items()}

    def profile_list(self) -> List[Profile]:
        return list(self.profiles.values())

    def hub(self) -> Profile:
        for p in self.profiles.values():
            if p.is_hub:
                return p
        raise RuntimeError("no hub profile")  # unreachable: __init__ guarantees one

    def get(self, name: str) -> Profile:
        profiles = self.profiles
        if name not in profiles:
            known = ", ".join(profiles) or "none"
            raise UnknownProfile(f"unknown profile '{name}' (known: {known})")
        return profiles[name]

    def put(self, p: Profile) -> None:
        self.data["profiles"][p.name] = p.to_dict()

    def remove(self, name: str) -> None:
        self.data["profiles"].pop(name, None)

    def rename(self, old: str, new: str) -> None:
        """Rename a profile, keeping its order and its config dir (the login is tied to the dir)."""
        if old == new:
            return
        items = list(self.data["profiles"].items())
        self.data["profiles"] = {(new if k == old else k): v for k, v in items}
        sch = self.settings.get("schedule") or {}
        if isinstance(sch.get("profiles"), list):
            sch["profiles"] = [new if n == old else n for n in sch["profiles"]]

    def rename_hub(self, new: str) -> None:
        self.rename(self.hub().name, new)

    def current(self, env: Optional[Dict[str, str]] = None) -> Optional[Profile]:
        """Profile of the Claude Code process we are running under (status line, slash command)."""
        env = os.environ if env is None else env
        cd = env.get("CLAUDE_CONFIG_DIR")
        if cd:
            want = normalize_dir(cd)
            for p in self.profiles.values():
                if p.config_dir is not None and normalize_dir(str(p.config_dir)) == want:
                    return p
            return None
        name = env.get("CAS_PROFILE")
        if name and name in self.profiles and self.profiles[name].is_hub:
            return self.profiles[name]
        return self.hub()
