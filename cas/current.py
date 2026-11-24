"""The account cas is on, where a fresh `claude` starts; shared between machines when `cas share` is on.

Limits belong to the account, not the machine. When one Mac moves to another account (`cas run`
picking one, a handoff), fresh sessions on the other Mac follow it, instead of starting on the
account that just ran out.

Each machine writes only its own file, so two machines never write the same file:
    local:   ~/.claude-accounts/state/_global.json    last_profile, last_profile_at
    shared:  <shared folder>/current/<host>.json      {"account": <account id>, "at": <time>}

The newest choice wins. Machines match accounts by account id, because the same account can have
a different profile name on each machine; an account this machine has no usable profile for is
skipped.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional, Tuple

from .config import Config, Profile, read_json, write_json_atomic
from .usage import load_state, save_state


def _shared_dir(cfg: Config) -> Optional[Path]:
    from . import shared  # local import: shared doesn't depend on this module

    store = shared.store_path(cfg.settings)
    return store / "current" if store else None


def _usable(p: Optional[Profile]) -> bool:
    return p is not None and p.enabled and p.logged_in()


def set_current(cfg: Config, p: Profile, now: Optional[float] = None) -> None:
    now = time.time() if now is None else now
    st = load_state("_global")
    st.update(last_profile=p.name, last_profile_at=now)
    save_state("_global", st)
    d, acct = _shared_dir(cfg), p.account_uuid
    if d is None or not acct:
        return
    from .shared import host

    try:
        d.mkdir(parents=True, exist_ok=True)
        write_json_atomic(d / f"{host()}.json", {"account": acct, "at": now}, 0o644)
    except OSError:
        pass  # the folder may be offline; the next choice publishes again


def get_current(cfg: Config) -> Tuple[Optional[Profile], Optional[str]]:
    """(profile, the other machine that chose it, or None when this one did). (None, None) if none is usable."""
    st = load_state("_global")
    picks: List[Tuple[float, Profile, Optional[str]]] = []
    name = st.get("last_profile")
    mine = cfg.profiles.get(name) if isinstance(name, str) else None
    if _usable(mine):
        try:
            picks.append((float(st.get("last_profile_at") or 0), mine, None))
        except (TypeError, ValueError):
            picks.append((0.0, mine, None))
    d = _shared_dir(cfg)
    others = list(d.glob("*.json")) if d is not None and d.is_dir() else []
    if others:
        from .shared import host

        by_account = {q.account_uuid: q for q in cfg.profile_list() if _usable(q)}
        for f in others:
            data = read_json(f, None)
            if f.stem == host() or not isinstance(data, dict):
                continue
            q = by_account.get(data.get("account"))
            try:
                at = float(data.get("at") or 0)
            except (TypeError, ValueError):
                continue
            if q is not None:
                picks.append((at, q, f.stem))
    if not picks:
        return None, None
    _, p, where = max(picks, key=lambda x: x[0])
    return p, where
