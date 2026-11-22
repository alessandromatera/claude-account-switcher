import json
import os
import time
import unittest

from cas import profiles as prof
from cas.config import Config, Profile
from tests.helpers import IsolatedHome


class ProfilesTest(IsolatedHome):
    def make(self, name="work"):
        p = Profile(name=name, config_dir=self.home / ".claude-accounts" / "profiles" / name)
        prof.create_dir(p)
        return p

    def test_create_links_and_seed(self):
        (self.hub / "settings.json").write_text(json.dumps({"model": "opus"}))
        (self.hub / "CLAUDE.md").write_text("# shared memory\n")
        (self.hub / "plugins").mkdir()
        p = self.make()
        for d in ("projects", "agents", "commands", "skills", "plugins"):
            self.assertTrue((p.dir / d).is_symlink(), d)
            self.assertEqual(os.path.realpath(p.dir / d), os.path.realpath(self.hub / d))
        self.assertFalse((p.dir / "todos").exists(), "optional dirs are linked only when the hub has them")
        self.assertFalse((p.dir / "settings.json").is_symlink(), "files are copied, never symlinked")
        self.assertEqual(self.read_json(p.dir / "settings.json"), {"model": "opus"})
        self.assertEqual((p.dir / "CLAUDE.md").read_text(), "# shared memory\n")
        seeded = self.read_json(p.global_json)
        self.assertTrue(seeded["hasCompletedOnboarding"])
        self.assertEqual(seeded["theme"], "dark")
        self.assertEqual(list(seeded["projects"]), ["/work/a"])
        self.assertNotIn("oauthAccount", seeded)
        self.assertEqual(oct(os.stat(p.dir).st_mode & 0o777), "0o700")

    def test_memory_written_in_profile_lands_in_hub(self):
        p = self.make()
        mem = p.dir / "projects" / "-some-project" / "memory"
        mem.mkdir(parents=True)
        (mem / "MEMORY.md").write_text("- fact")
        self.assertEqual((self.hub / "projects" / "-some-project" / "memory" / "MEMORY.md").read_text(), "- fact")

    def test_three_way_sync(self):
        (self.hub / "settings.json").write_text(json.dumps({"model": "opus", "a": 1}))
        p = self.make()
        # only the profile changed -> goes to hub
        self.write_json(p.dir / "settings.json", {"model": "sonnet", "a": 1})
        self.assertEqual(prof.sync_files(p), ["settings.json: work -> hub"])
        self.assertEqual(self.read_json(self.hub / "settings.json")["model"], "sonnet")
        # only the hub changed -> goes to profile
        self.write_json(self.hub / "settings.json", {"model": "sonnet", "a": 2})
        prof.sync_files(p)
        self.assertEqual(self.read_json(p.dir / "settings.json")["a"], 2)
        # both changed different keys -> merged
        self.write_json(self.hub / "settings.json", {"model": "sonnet", "a": 2, "hubkey": True})
        self.write_json(p.dir / "settings.json", {"model": "haiku", "a": 2})
        out = prof.sync_files(p)
        self.assertIn("merged", out[0])
        merged = self.read_json(self.hub / "settings.json")
        self.assertEqual(merged, {"model": "haiku", "a": 2, "hubkey": True})
        self.assertEqual(self.read_json(p.dir / "settings.json"), merged)
        self.assertEqual(prof.sync_files(p), [])

    def test_text_conflict_keeps_newer_and_backs_up(self):
        (self.hub / "CLAUDE.md").write_text("v1")
        p = self.make()
        (self.hub / "CLAUDE.md").write_text("hub edit")
        os.utime(self.hub / "CLAUDE.md", (time.time() - 100, time.time() - 100))
        (p.dir / "CLAUDE.md").write_text("profile edit")
        out = prof.sync_files(p)
        self.assertIn("backup", out[0])
        self.assertEqual((self.hub / "CLAUDE.md").read_text(), "profile edit")
        backups = list((self.home / ".claude-accounts" / "backups").iterdir())
        self.assertEqual(backups[0].read_text(), "hub edit")

    def test_real_dir_in_profile_is_merged_into_hub(self):
        p = self.make()
        (p.dir / "agents").unlink()
        (p.dir / "agents").mkdir()
        (p.dir / "agents" / "mine.md").write_text("agent")
        (self.hub / "agents" / "hub.md").write_text("hub agent")
        prof.ensure_links(p)
        self.assertTrue((p.dir / "agents").is_symlink())
        self.assertEqual(sorted(x.name for x in (self.hub / "agents").iterdir()), ["hub.md", "mine.md"])

    def test_mcp_sync_never_touches_unreadable_config(self):
        p = self.make()
        data = self.read_json(p.global_json)
        data["mcpServers"] = {"own": {"command": "y"}}
        data["oauthAccount"] = {"accountUuid": "acct-work"}
        self.write_json(p.global_json, data)
        hub = self.read_json(self.hub_json)
        hub["mcpServers"]["new"] = {"command": "z"}
        self.write_json(self.hub_json, hub)
        self.assertIn("new", prof.sync_mcp(p))
        after = self.read_json(p.global_json)
        self.assertEqual(set(after["mcpServers"]), {"own", "hubsrv", "new"})
        self.assertEqual(after["oauthAccount"]["accountUuid"], "acct-work")
        p.global_json.write_text("{broken")
        self.assertIsNone(prof.sync_mcp(p))
        self.assertEqual(p.global_json.read_text(), "{broken")

    def test_env_and_current_profile(self):
        p = self.make()
        cfg = Config.load()
        cfg.put(p)
        env = p.env({"CLAUDE_CONFIG_DIR": "/elsewhere", "PATH": "/bin"})
        self.assertEqual(env["CLAUDE_CONFIG_DIR"], str(p.config_dir))
        self.assertEqual(cfg.current(env).name, "work")
        self.assertNotIn("CLAUDE_CONFIG_DIR", cfg.hub().env({"CLAUDE_CONFIG_DIR": "/x"}))
        self.assertEqual(cfg.current({}).name, "default")
        self.assertIsNone(cfg.current({"CLAUDE_CONFIG_DIR": "/not/a/profile"}))


if __name__ == "__main__":
    unittest.main()
