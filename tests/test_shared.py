import io
import json
import os
import tempfile
import time
import unittest
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

from cas import cli, shared


class TwoMachines(unittest.TestCase):
    """Two homes with different usernames, one Dropbox-synced store (like a laptop and a desktop)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self.homes = {"laptop": root / "Users" / "alice", "desktop": root / "Users" / "bob"}
        self.store = root / "dropbox-store"
        for h in self.homes.values():
            (h / ".claude" / "projects").mkdir(parents=True)
            (h / "Dropbox" / "Projects" / "X").mkdir(parents=True)
        self._saved = {k: os.environ.get(k) for k in ("HOME", "CAS_HOME", "CAS_HUB_DIR", "CAS_HUB_JSON")}
        self.settings = {"shared_store": str(self.store), "dropbox_root": "~/Dropbox"}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._tmp.cleanup()

    @contextmanager
    def on(self, name):
        os.environ["HOME"] = str(self.homes[name])
        for k in ("CAS_HOME", "CAS_HUB_DIR", "CAS_HUB_JSON"):
            os.environ.pop(k, None)
        with mock.patch.object(shared, "host", return_value=name):
            yield self.homes[name]

    def mem(self, name, project="Projects/X"):
        h = self.homes[name]
        key = shared.sanitize(str(h / "Dropbox" / project))
        d = h / ".claude" / "projects" / key / "memory"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def sync(self, name, **kw):
        with self.on(name):
            return shared.sync(self.settings, **kw)

    def test_memory_follows_the_project_across_machines(self):
        a, b = self.mem("laptop"), self.mem("desktop")
        (a / "MEMORY.md").write_text("- [A fact](a.md)\n")
        (a / "a.md").write_text("from laptop")
        (b / "MEMORY.md").write_text("- [B fact](b.md)\n")
        (b / "b.md").write_text("from desktop")
        (self.homes["laptop"] / ".claude" / "CLAUDE.md").write_text("# me\n")

        self.sync("laptop")
        self.assertEqual((self.store / "memory" / "Projects-X" / "a.md").read_text(), "from laptop")
        acts = self.sync("desktop")
        self.assertTrue(any("merged" in x for x in acts), acts)
        self.sync("laptop")

        for d in (a, b):
            self.assertEqual(sorted(p.name for p in d.iterdir()), ["MEMORY.md", "a.md", "b.md"])
            self.assertEqual(set((d / "MEMORY.md").read_text().splitlines()),
                             {"- [A fact](a.md)", "- [B fact](b.md)"})
        self.assertEqual((self.homes["desktop"] / ".claude" / "CLAUDE.md").read_text(), "# me\n")

        # deletion propagates
        (b / "a.md").unlink()
        self.sync("desktop")
        self.sync("laptop")
        self.assertFalse((a / "a.md").exists())
        # a no-op sync changes nothing
        self.assertEqual(self.sync("laptop"), [])

    def test_conflict_keeps_both_versions(self):
        a, b = self.mem("laptop"), self.mem("desktop")
        (a / "MEMORY.md").write_text("- [Note](note.md)\n")
        (a / "note.md").write_text("v1")
        self.sync("laptop")
        self.sync("desktop")
        (a / "note.md").write_text("laptop edit")
        (b / "note.md").write_text("desktop edit")
        os.utime(a / "note.md", (time.time() - 100, time.time() - 100))
        self.sync("desktop")
        acts = self.sync("laptop")
        self.assertTrue(any("edited on both" in x for x in acts), acts)
        self.assertEqual((a / "note.md").read_text(), "desktop edit")
        self.assertEqual((a / "note.conflict-laptop.md").read_text(), "laptop edit")
        self.assertIn("note.conflict-laptop.md", (a / "MEMORY.md").read_text())
        self.sync("desktop")
        self.assertEqual((b / "note.conflict-laptop.md").read_text(), "laptop edit")

    def test_project_only_on_other_machine_is_created_here(self):
        (self.mem("laptop", "Projects/OnlyHere") / "MEMORY.md").write_text("- x\n")
        self.sync("laptop")
        self.sync("desktop")
        key = shared.sanitize(str(self.homes["desktop"] / "Dropbox" / "Projects/OnlyHere"))
        self.assertTrue((self.homes["desktop"] / ".claude" / "projects" / key / "memory" / "MEMORY.md").exists())

    def test_projects_outside_dropbox_and_synced_skills_stay_local(self):
        h = self.homes["laptop"] / ".claude"
        (h / "projects" / "-tmp-elsewhere" / "memory").mkdir(parents=True)
        (h / "projects" / "-tmp-elsewhere" / "memory" / "MEMORY.md").write_text("private")
        (h / "skills" / "synced" / "org_acct").mkdir(parents=True)
        (h / "skills" / "synced" / "org_acct" / "SKILL.md").write_text("account-managed")
        (h / "skills" / "mine").mkdir(parents=True)
        (h / "skills" / "mine" / "SKILL.md").write_text("my skill")
        target = self.homes["laptop"] / "Dropbox" / "Projects" / "tone"
        target.mkdir()
        os.symlink(str(target), str(h / "skills" / "tone"))
        (self.homes["desktop"] / "Dropbox" / "Projects" / "tone").mkdir()
        self.sync("laptop")
        self.assertFalse((self.store / "skills" / "synced").exists())
        self.assertFalse(any("elsewhere" in str(p) for p in self.store.rglob("*")))
        self.sync("desktop")
        fh = self.homes["desktop"] / ".claude"
        self.assertEqual((fh / "skills" / "mine" / "SKILL.md").read_text(), "my skill")
        self.assertTrue((fh / "skills" / "tone").is_symlink())
        self.assertEqual(os.path.realpath(fh / "skills" / "tone"),
                         os.path.realpath(self.homes["desktop"] / "Dropbox" / "Projects" / "tone"))

    def test_quick_hook_sync_only_touches_that_project(self):
        a = self.mem("laptop")
        other = self.mem("laptop", "Projects/Y")
        (a / "MEMORY.md").write_text("x")
        (other / "MEMORY.md").write_text("y")
        self.sync("laptop", quick=True, only_key=a.parent.name)
        self.assertTrue((self.store / "memory" / "Projects-X" / "MEMORY.md").exists())
        self.assertFalse((self.store / "memory" / "Projects-Y").exists())

    def test_hooks_install_remove_and_hook_entry_point_is_silent(self):
        with self.on("laptop") as h:
            settings = h / ".claude" / "settings.json"
            settings.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]}}))
            self.assertIn("installed", cli.set_share_hooks(True))
            data = json.loads(settings.read_text())
            self.assertEqual(len(data["hooks"]["Stop"]), 2)
            self.assertIn("sync --hook --quick", data["hooks"]["Stop"][1]["hooks"][0]["command"])
            self.assertIn("SessionStart", data["hooks"])
            self.assertTrue(cli.share_hooks_installed())
            self.assertIn("already", cli.set_share_hooks(True))
            cli.set_share_hooks(False)
            data = json.loads(settings.read_text())
            self.assertEqual(data["hooks"], {"Stop": [{"hooks": [{"type": "command", "command": "mine"}]}]})

            cfg = cli.Config({"settings": self.settings})
            (self.mem("laptop") / "MEMORY.md").write_text("z")
            with mock.patch("sys.stdin", io.StringIO(json.dumps({"transcript_path": "/x/y.jsonl", "cwd": "/"}))), \
                    redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.hook_sync(cfg, quick=False), 0)
            self.assertEqual(out.getvalue(), "", "SessionStart output would be injected into the conversation")
            self.assertTrue((self.store / "memory" / "Projects-X" / "MEMORY.md").exists())


if __name__ == "__main__":
    unittest.main()
