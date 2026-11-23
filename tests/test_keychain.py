import io
import json
import os
import subprocess
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cas import cli, keychain, statusline, usage
from cas.config import Config
from tests.helpers import IsolatedHome

REAL_ATTRS = '''keychain: "/Users/x/Library/Keychains/login.keychain-db"
version: 512
class: "genp"
attributes:
    0x00000007 <blob>="Claude Code-credentials-3b51c166"
    "acct"<blob>="x"
    "cdat"<timedate>=0x32303236313132323134343833335A00  "20261122144833Z\\000"
    "mdat"<timedate>=0x32303236313132323134343833335A00  "20261122144833Z\\000"
    "svce"<blob>="Claude Code-credentials-3b51c166"
'''


class FakeSecurity:
    """The `security` commands cas uses, against an in-memory login keychain."""

    def __init__(self, locked=True, settings="lock-on-sleep timeout=300s"):
        self.locked = locked
        self.settings = settings
        self.items = {}  # service -> last written (epoch)
        self.calls = []

    def __call__(self, args, timeout=10):
        self.calls.append(list(args))
        cmd = args[0]

        def done(rc, out="", err=""):
            return subprocess.CompletedProcess(["security", *args], rc, out, err)

        if cmd == "show-keychain-info":
            if self.locked:
                return done(36, err="security: SecKeychainCopySettings: User interaction is not allowed.")
            return done(0, err=f'Keychain "/Users/x/Library/Keychains/login.keychain-db" {self.settings}\n')
        if cmd == "set-keychain-settings":
            self.settings = "no-timeout"
            return done(0)
        svc = args[args.index("-s") + 1]
        if svc not in self.items:
            return done(44, err="security: SecKeychainSearchCopyNext: The specified item could not be found.")
        if cmd == "find-generic-password":
            stamp = time.strftime("%Y%m%d%H%M%S", time.gmtime(self.items[svc]))
            return done(0, f'attributes:\n    "mdat"<timedate>=0x00  "{stamp}Z\\000"\n')
        if cmd == "delete-generic-password":
            del self.items[svc]
            return done(0, "password has been deleted.\n")
        raise AssertionError(f"unexpected security call: {args}")


class KeychainTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(["add", "work", "--no-login"]), 0)
        self.p = Config.load().get("work")
        data = self.read_json(self.p.global_json)
        data["oauthAccount"] = {"accountUuid": "acct-work", "emailAddress": "w@example.com"}
        self.write_json(self.p.global_json, data)
        self.sec = FakeSecurity()
        for target, value in (("supported", mock.Mock(return_value=True)), ("_run", self.sec)):
            patcher = mock.patch.object(keychain, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def keychain_login(self, at):
        self.sec.items[self.p.keychain_service] = float(int(at))

    def file_login(self, at):
        f = self.p.dir / ".credentials.json"
        f.write_text(json.dumps({"claudeAiOauth": {"accessToken": "t"}}))
        os.utime(f, (at, at))

    def test_reads_when_the_keychain_copy_was_written(self):
        self.assertEqual(keychain._mdat(REAL_ATTRS), 1795358913.0)  # 2026-11-22 14:48:33 UTC
        self.assertIsNone(keychain._mdat("attributes:\n"))

    def test_login_states(self):
        L = keychain.Login
        self.assertTrue(L(True, True, 100.0, None).stranded, "only the locked keychain has it")
        self.assertFalse(L(False, True, 100.0, None).stranded, "unlocked: Claude Code reads it")
        self.assertTrue(L(True, True, 200.0, 100.0).stranded, "the newer copy is the locked one")
        self.assertFalse(L(True, True, 100.0, 200.5).stranded, "the file is newer: used while locked")
        self.assertTrue(L(True, True, 100.0, 200.5).stale_keychain_copy)
        self.assertFalse(L(True, True, 100.0, 100.9).stale_keychain_copy, "same second: can't tell")
        self.assertFalse(L(True, True, None, 100.0).stranded, "unknown age: leave it to Claude Code")
        self.assertFalse(L(None, False, None, 100.0).stranded)

    def test_stale_keychain_copy_is_removed_even_while_locked(self):
        now = time.time()
        self.keychain_login(now - 86400)
        self.file_login(now - 60)
        login, actions = keychain.settle(self.p)
        self.assertNotIn(self.p.keychain_service, self.sec.items)
        self.assertEqual(len(actions), 1)
        self.assertTrue(login.in_file and not login.in_keychain and not login.stranded)
        self.assertFalse((self.p.dir / keychain.CLAUDE_STORE_LOCK).exists(), "lock released")
        self.assertTrue((self.p.dir / ".credentials.json").exists(), "the newer login is never touched")

    def test_newer_keychain_login_and_a_saving_claude_code_are_left_alone(self):
        now = time.time()
        self.keychain_login(now - 60)
        self.file_login(now - 86400)
        self.assertEqual(keychain.settle(self.p)[1], [])
        self.assertIn(self.p.keychain_service, self.sec.items)
        self.assertTrue((self.p.dir / ".credentials.json").exists())

        self.keychain_login(now - 86400)
        self.file_login(now - 60)
        (self.p.dir / keychain.CLAUDE_STORE_LOCK).mkdir()  # Claude Code is saving a renewed login
        self.assertEqual(keychain.settle(self.p)[1], [])
        self.assertIn(self.p.keychain_service, self.sec.items)

    def test_launch_asks_to_unlock_only_when_the_login_is_stranded(self):
        self.keychain_login(time.time())
        style = cli.Style(False)
        with mock.patch.object(keychain, "unlock") as unlock, redirect_stdout(io.StringIO()) as out:
            cli.ensure_login(self.p, style, interactive=False)
        unlock.assert_not_called()
        self.assertIn("security unlock-keychain", out.getvalue())

        def unlocked():
            self.sec.locked = False
            return True

        answers = ["", "y"]  # unlock: default yes; keep it unlocked: asked, default no
        with mock.patch.object(keychain, "unlock", side_effect=unlocked) as unlock, \
                mock.patch("sys.stdin.isatty", return_value=True), \
                mock.patch("builtins.input", side_effect=lambda *_: answers.pop(0)), \
                redirect_stdout(io.StringIO()) as out:
            cli.ensure_login(self.p, style, interactive=True)
        unlock.assert_called_once()
        self.assertIn("after 5 min idle and when the Mac sleeps", out.getvalue())
        self.assertEqual(self.sec.settings, "no-timeout")
        self.assertIn(["set-keychain-settings"], self.sec.calls)

        self.sec.calls.clear()
        with mock.patch.object(keychain, "unlock") as unlock, redirect_stdout(io.StringIO()) as out:
            cli.ensure_login(self.p, style, interactive=True)  # unlocked now: nothing to do
        unlock.assert_not_called()
        self.assertEqual(out.getvalue(), "")

    def test_ping_does_not_run_claude_on_a_stranded_login(self):
        self.keychain_login(time.time())
        cfg = Config.load()
        with mock.patch.object(cli, "find_claude", return_value="/bin/claude"), \
                mock.patch.object(cli.subprocess, "run") as run, redirect_stdout(io.StringIO()) as out:
            self.assertFalse(cli.do_ping(cfg, self.p, None, False, False))
        run.assert_not_called()
        self.assertIn("locked", out.getvalue())

    def test_status_line_warns_and_checks_at_most_once_a_minute(self):
        self.keychain_login(time.time())
        cfg = Config.load()
        out = statusline.main("{}", cfg, self.p.env({}))
        self.assertIn("Keychain locked", out)
        n = len(self.sec.calls)
        statusline.main("{}", cfg, self.p.env({}))
        self.assertEqual(len(self.sec.calls), n, "cached")

        self.sec.locked = False
        st = usage.load_state("work")
        st["keychain_check"]["at"] = 0
        usage.save_state("work", st)
        self.assertNotIn("Keychain", statusline.main("{}", cfg, self.p.env({})))

    def test_live_usage_names_the_locked_keychain(self):
        self.keychain_login(time.time())
        with mock.patch.object(usage, "read_oauth", return_value=None):
            snap, err = usage.fetch_live(self.p)
        self.assertIsNone(snap)
        self.assertIn("locked Keychain", err)


if __name__ == "__main__":
    unittest.main()
