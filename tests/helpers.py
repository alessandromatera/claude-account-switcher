import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

PRO_ACCOUNT = {
    "accountUuid": "acct-hub",
    "emailAddress": "hub@example.com",
    "organizationType": "claude_pro",
    "billingType": "stripe_subscription",
}


class IsolatedHome(unittest.TestCase):
    """Every test gets a throwaway HOME with a fake ~/.claude hub."""

    def setUp(self):
        # Never touch the real macOS Keychain; tests/test_keychain.py fakes `security` instead.
        kc = mock.patch("cas.keychain.supported", return_value=False)
        kc.start()
        self.addCleanup(kc.stop)
        # Never spawn real background usage fetches from the status line.
        bg = mock.patch("cas.statusline.refresh_others")
        bg.start()
        self.addCleanup(bg.stop)
        self._tmp = tempfile.TemporaryDirectory()
        self.home = Path(self._tmp.name)
        self._env = {k: os.environ.get(k) for k in
                     ("HOME", "CAS_HOME", "CAS_HUB_DIR", "CAS_HUB_JSON", "CLAUDE_CONFIG_DIR", "CAS_PROFILE",
                      "CLAUDECODE")}
        os.environ["HOME"] = str(self.home)
        for k in ("CAS_HOME", "CAS_HUB_DIR", "CAS_HUB_JSON", "CLAUDE_CONFIG_DIR", "CAS_PROFILE", "CLAUDECODE"):
            os.environ.pop(k, None)
        self.hub = self.home / ".claude"
        self.hub.mkdir()
        (self.hub / "projects").mkdir()
        self.hub_json = self.home / ".claude.json"
        self.write_json(self.hub_json, {"oauthAccount": PRO_ACCOUNT, "theme": "dark",
                                        "mcpServers": {"hubsrv": {"command": "x"}},
                                        "projects": {"/work/a": {"hasTrustDialogAccepted": True},
                                                     "/work/b": {"hasTrustDialogAccepted": False}}})

    def tearDown(self):
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    @staticmethod
    def write_json(path, data):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2))

    @staticmethod
    def read_json(path):
        return json.loads(Path(path).read_text())


# Last messages of a subagent, as Claude Code writes them (see cas/agents.py).
AGENT_LIMIT = {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit",
               "message": {"content": [{"type": "text", "text": "You've hit your 5-hour limit · resets 3:30pm"}]}}
AGENT_INTERRUPTED = {"type": "user", "message": {"content": [{"type": "text", "text": "[Request interrupted by user]"}]}}
AGENT_WORKING = {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash",
                                                               "input": {"command": "make"}}]}}
AGENT_DONE = {"type": "assistant", "message": {"content": [{"type": "text", "text": "All done."}]}}


def iso(ts):
    import datetime
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat()


def write_agent(transcript, aid, last, finished, at, depth=1, description=None):
    """One subagent of the conversation whose transcript is `transcript`, last active at `at`."""
    from cas import agents

    d = agents.subagents_dir(Path(transcript))
    d.mkdir(parents=True, exist_ok=True)
    meta = {"agentType": "general-purpose", "isBuiltIn": True, "description": description or f"task {aid}",
            "toolUseId": f"toolu_{aid}", "spawnDepth": depth, "requestShape": "background"}
    if finished:
        meta["finished"] = True
    (d / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
    entries = [{"type": "user", "agentId": aid, "timestamp": iso(at - 60), "message": {"content": "do the task"}},
               dict(last, agentId=aid, timestamp=iso(at))]
    path = d / f"agent-{aid}.jsonl"
    path.write_text("".join(json.dumps(e) + "\n" for e in entries))
    os.utime(path, (at, at))
    return path
