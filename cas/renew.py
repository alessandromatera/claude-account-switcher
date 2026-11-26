"""Renewing an expired login the way Claude Code does, so cas can read an idle account's usage.

An account's access token lasts about 8 hours. Claude Code renews it when it runs on that
account; until then the usage endpoint refuses the old one, and that account's figures go
stale ("11h old"). So when a token has expired, cas renews it: the request Claude Code makes
(the login's refresh token, Claude Code's client id and the login's scopes, sent to
platform.claude.com), with the result saved where Claude Code keeps the login, in its shape.

Renewing also replaces the refresh token: the old one stops working. Two renewals at once
would leave one side with a dead login, so cas renews only when no Claude Code session is
running on the account, and while it holds Claude Code's own lock for saving a login. Tokens
are never printed or logged; `security` gets the login on stdin, as Claude Code passes it.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import __version__, keychain
from .config import Profile, cas_home, keychain_account, read_json, write_json_atomic

TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"  # Claude Code's
DEFAULT_SCOPES = ["user:profile", "user:inference", "user:sessions:claude_code", "user:mcp_servers",
                  "user:file_upload", "user:plugins"]
STDIN_LIMIT = 4032  # the longest line `security -i` reads (Claude Code keeps to the same)
CREDENTIALS = ".credentials.json"


# --------------------------------------------------------------------------- where the login is

def keychain_read(service: str) -> Optional[str]:
    try:
        r = subprocess.run(["security", "find-generic-password", "-a", keychain_account(), "-w", "-s", service],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout.strip() if r.returncode == 0 else None


def keychain_write(service: str, text: str) -> bool:
    """Save over the keychain item, passing it on stdin (never on the command line, where `ps` shows it)."""
    line = f'add-generic-password -U -a "{keychain_account()}" -s "{service}" -X "{text.encode().hex()}"\n'
    if len(line) > STDIN_LIMIT:
        return False
    try:
        r = subprocess.run(["security", "-i"], input=line, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def _parse(raw: Optional[str]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        try:  # stored hex-encoded
            data = json.loads(bytes.fromhex(raw).decode("utf-8"))
        except ValueError:
            return None
    return data if isinstance(data, dict) else None


def read_store(profile: Profile) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    """("keychain" | "file", everything stored there), read in Claude Code's order; (None, None) if neither."""
    data = _parse(keychain_read(profile.keychain_service))
    if data is not None:
        return "keychain", data
    data = read_json(profile.dir / CREDENTIALS, None)
    return ("file", data) if isinstance(data, dict) else (None, None)


def write_store(profile: Profile, where: str, data: Dict[str, Any]) -> bool:
    """Save the login where it was read from, and check it reads back. When the keychain refuses it,
    the file takes it and the keychain's old copy goes (it would be read first, and it no longer works)."""
    text = json.dumps(data, separators=(",", ":"))
    token = (data.get("claudeAiOauth") or {}).get("accessToken")
    if where == "keychain" and keychain_write(profile.keychain_service, text):
        back = _parse(keychain_read(profile.keychain_service)) or {}
        if (back.get("claudeAiOauth") or {}).get("accessToken") == token:
            return True
    try:
        write_json_atomic(profile.dir / CREDENTIALS, data, 0o600)
    except OSError:
        return False
    if where == "keychain":
        keychain.delete(profile.keychain_service)
    return True


# --------------------------------------------------------------------------- renewing

def claude_running(config_dir: Path) -> Optional[bool]:
    """Whether a Claude Code session runs on this config dir, from `<dir>/sessions/<pid>.json`.
    None when it can't tell (a Claude Code too old to keep that registry)."""
    reg = config_dir / "sessions"
    if not reg.is_dir():
        return None
    for f in reg.glob("*.json"):
        try:
            pid = int(json.loads(f.read_text())["pid"])
        except (OSError, ValueError, KeyError, TypeError):
            continue
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            continue  # left behind by a Claude Code that is gone
        except OSError:
            pass  # alive, but not ours to signal
        return True
    return False


def expired(oauth: Dict[str, Any], now: float, margin: float = 60) -> bool:
    exp = oauth.get("expiresAt")
    return isinstance(exp, (int, float)) and exp / 1000.0 < now + margin


def _log(profile: Profile, msg: str) -> None:
    try:
        log = cas_home() / "logs" / "renew.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        with open(log, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {profile.name}: {msg}\n")
    except OSError:
        pass


def _post(refresh_token: str, scopes: Any, timeout: float) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    body = {"grant_type": "refresh_token", "refresh_token": refresh_token, "client_id": CLIENT_ID,
            "scope": " ".join(scopes if isinstance(scopes, list) and scopes else DEFAULT_SCOPES)}
    req = urllib.request.Request(TOKEN_URL, data=json.dumps(body).encode(), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": f"claude-account-switcher/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        if e.code in (400, 401):
            return None, "the login can't be renewed any more: log in again on this account"
        return None, f"renewal answered HTTP {e.code}"
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, f"renewal unreachable ({e.__class__.__name__})"
    if not isinstance(data, dict) or not data.get("access_token") or not isinstance(data.get("expires_in"), (int, float)):
        return None, "renewal answered without a token"
    return data, None


def renew(profile: Profile, now: Optional[float] = None, timeout: float = 10.0
          ) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Renew the profile's expired login and save it. (the renewed claudeAiOauth, None) or (None, why not)."""
    now = time.time() if now is None else now
    running = claude_running(profile.dir)
    if running is None:
        return None, "can't tell whether Claude Code runs on it (it keeps no sessions/ folder), so cas won't renew"
    if running:
        return None, "Claude Code is open on it and renews it itself"
    with keychain.claude_store_lock(profile.dir) as held:
        if not held:
            return None, "Claude Code is saving this login right now"
        where, data = read_store(profile)
        oauth = (data or {}).get("claudeAiOauth")
        if where is None or not isinstance(oauth, dict) or not oauth.get("refreshToken"):
            return None, "not logged in"
        if not expired(oauth, now):
            return oauth, None  # renewed meanwhile
        got, err = _post(oauth["refreshToken"], oauth.get("scopes"), timeout)
        if got is None:
            _log(profile, f"not renewed: {err}")
            return None, err
        done = time.time()
        new = dict(oauth, accessToken=got["access_token"], expiresAt=int((done + got["expires_in"]) * 1000))
        if got.get("refresh_token"):
            new["refreshToken"] = got["refresh_token"]
        if isinstance(got.get("refresh_token_expires_in"), (int, float)):
            new["refreshTokenExpiresAt"] = int((done + got["refresh_token_expires_in"]) * 1000)
        if isinstance(got.get("scope"), str) and got["scope"].split():
            new["scopes"] = got["scope"].split()
        if not write_store(profile, where, dict(data, claudeAiOauth=new)):
            _log(profile, "renewed but not saved: log in again on this account")
            return None, "renewed, but the login could not be saved: log in again on this account"
        _log(profile, f"renewed ({'Keychain' if where == 'keychain' else CREDENTIALS}), "
                      f"good until {time.strftime('%H:%M', time.localtime(new['expiresAt'] / 1000))}")
        return new, None
