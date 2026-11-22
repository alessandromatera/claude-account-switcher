import json
import os
import tempfile
import unittest
from pathlib import Path

PRO_ACCOUNT = {
    "accountUuid": "acct-hub",
    "emailAddress": "hub@example.com",
    "organizationType": "claude_pro",
    "billingType": "stripe_subscription",
}


class IsolatedHome(unittest.TestCase):
    """Every test gets a throwaway HOME with a fake ~/.claude hub."""

    def setUp(self):
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
