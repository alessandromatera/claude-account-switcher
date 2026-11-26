import io
import json
import os
import time
import unittest
import urllib.error
from unittest import mock

from cas import renew, usage
from cas.config import Config
from tests.helpers import IsolatedHome

GOT = {"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 28800,
       "scope": "user:inference user:profile"}


def login(expires_at, **extra):
    return {"claudeAiOauth": dict({"accessToken": "old-access", "refreshToken": "old-refresh",
                                   "expiresAt": int(expires_at * 1000), "scopes": ["user:inference", "user:profile"],
                                   "subscriptionType": "pro", "rateLimitTier": "default"}, **extra),
            "mcpOAuth": {"srv": {"token": "keep"}}}


class RenewTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.p = Config.load().get("default")
        (self.p.dir / "sessions").mkdir()  # Claude Code's registry: no session running
        self.cred = self.p.dir / ".credentials.json"

    def post(self, got=GOT, err=None):
        return mock.patch("cas.renew._post", return_value=(got, err))

    def test_renews_an_expired_login_in_the_file_and_keeps_the_rest(self):
        self.write_json(self.cred, login(time.time() - 3600))
        with self.post() as post:
            new, err = renew.renew(self.p)
        self.assertIsNone(err)
        self.assertEqual(post.call_args[0][:2], ("old-refresh", ["user:inference", "user:profile"]))
        saved = self.read_json(self.cred)
        o = saved["claudeAiOauth"]
        self.assertEqual((o["accessToken"], o["refreshToken"], o["subscriptionType"]),
                         ("new-access", "new-refresh", "pro"))
        self.assertAlmostEqual(o["expiresAt"] / 1000, time.time() + 28800, delta=5)
        self.assertEqual(saved["mcpOAuth"], {"srv": {"token": "keep"}})
        self.assertEqual(oct(self.cred.stat().st_mode & 0o777), "0o600")
        self.assertEqual(new["accessToken"], "new-access")
        self.assertIn("renewed", (self.home / ".claude-accounts" / "logs" / "renew.log").read_text())
        self.assertNotIn("new-access", (self.home / ".claude-accounts" / "logs" / "renew.log").read_text())

    def test_saves_to_the_keychain_when_the_login_is_there(self):
        store = {"v": json.dumps(login(time.time() - 3600))}

        def write(service, text):
            store["v"] = text
            return True

        with mock.patch("cas.renew.keychain_read", side_effect=lambda s: store["v"]), \
                mock.patch("cas.renew.keychain_write", side_effect=write), self.post():
            _, err = renew.renew(self.p)
        self.assertIsNone(err)
        self.assertEqual(json.loads(store["v"])["claudeAiOauth"]["accessToken"], "new-access")
        self.assertFalse(self.cred.exists())

    def test_keychain_refuses_then_the_file_takes_it_and_the_old_copy_goes(self):
        with mock.patch("cas.renew.keychain_read", return_value=json.dumps(login(time.time() - 3600))), \
                mock.patch("cas.renew.keychain_write", return_value=False), \
                mock.patch("cas.keychain.delete", return_value=True) as delete, self.post():
            _, err = renew.renew(self.p)
        self.assertIsNone(err)
        self.assertEqual(self.read_json(self.cred)["claudeAiOauth"]["refreshToken"], "new-refresh")
        delete.assert_called_once_with(self.p.keychain_service)

    def test_never_while_claude_code_runs_on_the_account(self):
        self.write_json(self.cred, login(time.time() - 3600))
        self.write_json(self.p.dir / "sessions" / f"{os.getpid()}.json", {"pid": os.getpid(), "sessionId": "s"})
        with self.post() as post:
            new, err = renew.renew(self.p)
        self.assertIsNone(new)
        self.assertIn("open on it", err)
        post.assert_not_called()
        # one left behind by a Claude Code that is gone doesn't count
        self.write_json(self.p.dir / "sessions" / f"{os.getpid()}.json", {"pid": 2 ** 22 + 7, "sessionId": "s"})
        with self.post():
            self.assertIsNone(renew.renew(self.p)[1])

    def test_never_when_it_cant_tell_or_claude_code_is_saving(self):
        self.write_json(self.cred, login(time.time() - 3600))
        os.rmdir(self.p.dir / "sessions")
        with self.post() as post:
            self.assertIn("can't tell", renew.renew(self.p)[1])
        (self.p.dir / "sessions").mkdir()
        (self.p.dir / ".storage-write.lock").mkdir()
        with self.post() as post:
            self.assertIn("saving", renew.renew(self.p)[1])
        post.assert_not_called()

    def test_a_refused_renewal_changes_nothing(self):
        self.write_json(self.cred, login(time.time() - 3600))
        before = self.cred.read_text()
        with self.post(None, "the login can't be renewed any more: log in again on this account"):
            new, err = renew.renew(self.p)
        self.assertIsNone(new)
        self.assertIn("log in again", err)
        self.assertEqual(self.cred.read_text(), before)

    def test_a_login_still_valid_is_left_alone(self):
        self.write_json(self.cred, login(time.time() + 3600))
        with self.post() as post:
            new, err = renew.renew(self.p)
        self.assertEqual(new["accessToken"], "old-access")
        post.assert_not_called()

    def test_the_request_is_claude_codes(self):
        seen = {}

        def urlopen(req, timeout):
            seen["url"], seen["body"] = req.full_url, json.loads(req.data)
            return io.BytesIO(json.dumps(GOT).encode())

        with mock.patch("urllib.request.urlopen", side_effect=urlopen):
            got, err = REAL_POST("rt", ["a", "b"], 5)
        self.assertIsNone(err)
        self.assertEqual(seen["url"], "https://platform.claude.com/v1/oauth/token")
        self.assertEqual(seen["body"], {"grant_type": "refresh_token", "refresh_token": "rt",
                                        "client_id": renew.CLIENT_ID, "scope": "a b"})
        bad = urllib.error.HTTPError(renew.TOKEN_URL, 400, "invalid_grant", {}, io.BytesIO(b"{}"))
        with mock.patch("urllib.request.urlopen", side_effect=bad):
            self.assertIn("log in again", REAL_POST("rt", [], 5)[1])

    def test_live_usage_renews_first_or_says_why_not(self):
        self.write_json(self.cred, login(time.time() - 3600))
        snap, err = usage.fetch_live(self.p, renew_login=False)
        self.assertIn("renew_logins", err)
        with mock.patch("cas.renew.renew", return_value=(None, "Claude Code is open on it and renews it itself")):
            snap, err = usage.fetch_live(self.p)
        self.assertIsNone(snap)
        self.assertIn("not renewed: Claude Code is open on it", err)


REAL_POST = renew._post  # before IsolatedHome replaces it

if __name__ == "__main__":
    unittest.main()
