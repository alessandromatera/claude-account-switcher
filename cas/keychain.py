"""Where each account's login is stored, and keeping it usable while the macOS Keychain is locked.

Claude Code saves a login in the login keychain ("Claude Code-credentials[-<hash>]"). When it
can't write there, it uses <config dir>/.credentials.json instead. When it reads, the keychain
comes first.

Over SSH, or in a tmux started from SSH, the login keychain is often locked: macOS unlocks it
when you log in at the Mac and can lock it again (sleep, idle timeout, logout). A locked
keychain can't be read without a password prompt, and an SSH session can't show one. That is
how logins go missing:

  - A login saved while the keychain was unlocked can't be read once it locks: Claude Code
    says "Not logged in". A session started before the lock keeps going until its token expires,
    then can't renew it: Claude Code won't move a login out of the keychain into the file.
  - A login made while the keychain is locked goes to the file, but the keychain keeps the old
    one. Once the keychain unlocks, Claude Code reads that stale copy first.

So before Claude Code starts, cas deletes a keychain copy that is older than the file (this
works while the keychain is locked). When the newest login is in the locked keychain, cas
offers to unlock it; macOS asks for the password itself. cas compares the two copies by when
they were written, so it never reads the secret.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from calendar import timegm
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Tuple

from .config import Profile, keychain_account

LOCKED = 36  # `security` exit status for errSecInteractionNotAllowed: locked, and no way to prompt
NOT_FOUND = 44  # errSecItemNotFound
# Claude Code holds this lock (proper-lockfile: a directory) while it saves a renewed login.
CLAUDE_STORE_LOCK = ".storage-write.lock"


def supported() -> bool:
    return sys.platform == "darwin" and shutil.which("security") is not None


def _run(args: List[str], timeout: float = 10) -> Optional[subprocess.CompletedProcess]:
    try:
        return subprocess.run(["security", *args], capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return None


def is_locked() -> Optional[bool]:
    """Whether the default (login) keychain is locked; None if unknown. Claude Code checks it the same way."""
    r = _run(["show-keychain-info"])
    if r is None:
        return None
    if r.returncode == LOCKED:
        return True
    return False if r.returncode == 0 else None


def _mdat(text: str) -> Optional[float]:
    m = re.search(r'"mdat"<timedate>=\S*\s+"(\d{14})Z', text)
    if not m:
        return None
    try:
        return float(timegm(time.strptime(m.group(1), "%Y%m%d%H%M%S")))
    except ValueError:
        return None


def item(service: str) -> Tuple[Optional[bool], Optional[float]]:
    """(the keychain holds this login, when it was last written). Reads attributes only, which
    works while the keychain is locked. None means unknown."""
    r = _run(["find-generic-password", "-a", keychain_account(), "-s", service])
    if r is None:
        return None, None
    if r.returncode == 0:
        return True, _mdat(r.stdout)
    if r.returncode == NOT_FOUND:
        return False, None
    return None, None


def delete(service: str) -> bool:
    r = _run(["delete-generic-password", "-a", keychain_account(), "-s", service])
    return r is not None and r.returncode in (0, NOT_FOUND)


@dataclass
class Login:
    """Where a profile's login is stored right now."""
    locked: Optional[bool]  # the login keychain (only asked when it holds this login)
    in_keychain: Optional[bool]
    keychain_at: Optional[float]  # when the keychain copy was last written (1 s resolution)
    file_at: Optional[float]  # mtime of .credentials.json; None when there is none

    @property
    def in_file(self) -> bool:
        return self.file_at is not None

    @property
    def stale_keychain_copy(self) -> bool:
        """Both hold a login and the keychain's is older: it takes over once the keychain unlocks."""
        return bool(self.in_keychain and self.file_at is not None and self.keychain_at is not None
                    and int(self.file_at) > self.keychain_at)

    @property
    def stranded(self) -> bool:
        """The newest login is in the locked keychain: Claude Code can't read it or renew it."""
        if not (self.locked and self.in_keychain):
            return False
        if self.file_at is None:
            return True
        return self.keychain_at is not None and self.keychain_at > int(self.file_at)

    def where(self) -> str:
        if self.in_keychain and self.in_file:
            return "Keychain and .credentials.json"
        if self.in_keychain:
            return "Keychain" + (" (locked)" if self.locked else "")
        return ".credentials.json" if self.in_file else "nowhere"


def check(profile: Profile) -> Optional[Login]:
    """None when there is no keychain to worry about (not macOS)."""
    if not supported():
        return None
    has, at = item(profile.keychain_service)
    try:
        file_at: Optional[float] = (profile.dir / ".credentials.json").stat().st_mtime
    except OSError:
        file_at = None
    return Login(is_locked() if has else None, has, at, file_at)


@contextmanager
def claude_store_lock(config_dir: Path) -> Iterator[bool]:
    """Take Claude Code's own credential-store lock, so it can't save a renewed login meanwhile.
    Yields False when it's held (Claude Code is saving right now) or can't be taken."""
    lock = config_dir / CLAUDE_STORE_LOCK
    try:
        os.mkdir(lock)
    except OSError:
        yield False
        return
    try:
        yield True
    finally:
        try:
            os.rmdir(lock)
        except OSError:
            pass


def settle(profile: Profile) -> Tuple[Optional[Login], List[str]]:
    """Leave one login per profile: delete the keychain copy when the file holds a newer one.

    Returns the login state afterwards and what changed. Never asks anything.
    """
    login = check(profile)
    if login is None or not login.stale_keychain_copy:
        return login, []
    with claude_store_lock(profile.dir) as held:
        if not held:
            return login, []
        again = check(profile)
        if again is None or not again.stale_keychain_copy or not delete(profile.keychain_service):
            return again, []
    return check(profile), ["removed an older copy of the login from the Keychain (the newer one, in "
                            ".credentials.json, stays)"]


def unlock() -> bool:
    """`security unlock-keychain` on this terminal: macOS asks for the password, cas never sees it."""
    try:
        return subprocess.call(["security", "unlock-keychain"]) == 0
    except (OSError, KeyboardInterrupt):
        return False


def autolock() -> Optional[str]:
    """When the unlocked login keychain locks itself again ("after 5 min idle", "on sleep"), or None."""
    r = _run(["show-keychain-info"])
    if r is None or r.returncode != 0:
        return None
    text = (r.stdout or "") + (r.stderr or "")
    how = []
    m = re.search(r"timeout=(\d+)s", text)
    if m and "no-timeout" not in text:
        s = int(m.group(1))
        how.append(f"after {s // 60} min idle" if s >= 60 else f"after {s}s idle")
    if "lock-on-sleep" in text:
        how.append("when the Mac sleeps")
    return " and ".join(how) or None


def keep_unlocked() -> bool:
    """No idle timeout, no lock on sleep: the keychain stays unlocked until logout or restart."""
    r = _run(["set-keychain-settings"])
    return r is not None and r.returncode == 0
