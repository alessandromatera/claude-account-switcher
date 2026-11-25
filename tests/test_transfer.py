import io
import json
import os
import tarfile
import tempfile
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from cas import agents, shared, transfer
from tests.helpers import AGENT_INTERRUPTED, write_agent

SID = "5f0c2a8e-1111-4c2b-9d7e-0123456789ab"


class MoveBetweenMachines(unittest.TestCase):
    """A laptop (alice) and a desktop (bob) with one synced folder; the project is at ~/code/X on both."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(os.path.realpath(self._tmp.name))  # Claude Code records resolved paths
        self.homes = {"laptop": root / "Users" / "alice", "desktop": root / "Users" / "bob"}
        self.store = root / "synced"
        for h in self.homes.values():
            (h / ".claude" / "projects").mkdir(parents=True)
            (h / "code" / "X").mkdir(parents=True)
        self._saved = {k: os.environ.get(k) for k in ("HOME", "CAS_HOME", "CAS_HUB_DIR", "CAS_HUB_JSON")}
        self.settings = {"shared_store": str(self.store), "projects_root": "~/code"}

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

    def project(self, name):
        return str(self.homes[name] / "code" / "X")

    def transcript(self, name, lines):
        t = self.homes[name] / ".claude" / "projects" / transfer.sanitize(self.project(name)) / f"{SID}.jsonl"
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text("".join(json.dumps(x) + "\n" for x in lines))
        return t

    def laptop_conversation(self):
        alice, cwd = str(self.homes["laptop"]), self.project("laptop")
        memory = f"{alice}/.claude/projects/{transfer.sanitize(cwd)}/memory/notes.md"
        t = self.transcript("laptop", [
            {"type": "user", "cwd": cwd, "sessionId": SID, "timestamp": "2026-11-25T09:00:00Z",
             "message": {"content": "fix the parser"}},
            {"type": "assistant", "cwd": cwd, "sessionId": SID, "timestamp": "2026-11-25T09:01:00Z",
             "message": {"content": [
                 {"type": "tool_use", "name": "Read", "input": {"file_path": f"{cwd}/src/parse.py"}},
                 {"type": "text", "text": f"see {alice}/todo.txt, {memory}, {cwd}-old/a.py and /Volumes/v{cwd}"}]}},
            {"type": "ai-title", "aiTitle": "Fix the parser", "sessionId": SID},
        ])
        write_agent(t, "a1", AGENT_INTERRUPTED, False, time.time() - 300, description="survey")
        fh = self.homes["laptop"] / ".claude" / "file-history" / SID
        fh.mkdir(parents=True)
        (fh / "abc123@v1").write_text(f"a backup that mentions {cwd}")
        return t

    def send(self):
        with self.on("laptop"):
            t = self.laptop_conversation()
            return transfer.pack(self.settings, SID, t, label=transfer.title(t), still_open=False, profile="work")

    def test_conversation_moves_with_its_agents_and_paths(self):
        pkg = self.send()
        self.assertEqual((pkg.manifest["project"], pkg.manifest["host"], pkg.label), ("X", "laptop", "Fix the parser"))
        with self.on("laptop"):
            self.assertEqual(transfer.pending(self.settings), [], "nothing from the other machines here")
            self.assertEqual(len(transfer.pending(self.settings, own=True)), 1)
        bob, cwd = str(self.homes["desktop"]), self.project("desktop")
        with self.on("desktop"):
            (waiting,) = transfer.pending(self.settings)
            self.assertEqual(str(waiting.local_dir(self.settings)), cwd)
            self.assertEqual(len(transfer.waiting_here(self.settings, cwd)), 1)
            self.assertEqual(transfer.waiting_here(self.settings, bob), [])
            dest, actions = transfer.unpack(self.settings, waiting, cwd)

            self.assertEqual(dest, self.homes["desktop"] / ".claude" / "projects" / transfer.sanitize(cwd) / f"{SID}.jsonl")
            lines = [json.loads(x) for x in dest.read_text().splitlines()]
            self.assertEqual({x.get("cwd") for x in lines[:2]}, {cwd})
            self.assertEqual(lines[1]["message"]["content"][0]["input"]["file_path"], f"{cwd}/src/parse.py")
            text = lines[1]["message"]["content"][1]["text"]
            self.assertIn(f"{bob}/todo.txt", text)
            self.assertIn(f"{bob}/.claude/projects/{transfer.sanitize(cwd)}/memory/notes.md", text)
            self.assertIn(f"{bob}/code/X-old/a.py", text, "another folder in the same home: moved with the home")
            self.assertIn(f"/Volumes/v{self.project('laptop')}", text, "a path that only ends the same")
            self.assertIn(f"paths moved from {self.project('laptop')} to {cwd}", actions)

            backup = self.homes["desktop"] / ".claude" / "file-history" / SID / "abc123@v1"
            self.assertIn(self.project("laptop"), backup.read_text(), "file backups are content, left as they are")
            (agent,) = agents.session_agents(dest)
            self.assertEqual((agent.name(), agent.state()), ("survey", "interrupted"))
            self.assertEqual(list((self.store / "handoffs").iterdir()), [], "taken out of the shared folder")

    def test_waits_for_the_whole_archive(self):
        pkg = self.send()
        with self.on("desktop"):
            (waiting,) = transfer.pending(self.settings)
            data = pkg.archive.read_bytes()
            pkg.archive.write_bytes(data[: len(data) // 2])
            self.assertIn("still arriving", waiting.problem())
            with self.assertRaises(ValueError):
                transfer.unpack(self.settings, waiting, self.project("desktop"))
            pkg.archive.unlink()
            self.assertIn("hasn't arrived", waiting.problem())
            pkg.archive.with_name(f".{pkg.archive.name}.icloud").write_text("")
            self.assertIn("iCloud", waiting.problem())

    def test_refuses_an_archive_that_writes_elsewhere(self):
        pkg = self.send()
        with tarfile.open(pkg.archive, "w:gz") as tar:
            for name in ("conversation.jsonl", "conversation/../../../evil"):
                ti = tarfile.TarInfo(name)
                ti.size = 2
                tar.addfile(ti, io.BytesIO(b"{}"))
        pkg.manifest.update(sha256=transfer._sha(pkg.archive), size=pkg.archive.stat().st_size)
        pkg.path.write_text(json.dumps(pkg.manifest))
        with self.on("desktop"):
            (waiting,) = transfer.pending(self.settings)
            with self.assertRaises(ValueError):
                transfer.unpack(self.settings, waiting, self.project("desktop"))
        self.assertFalse(any(p.name == "evil" for p in Path(self._tmp.name).rglob("evil")))
        self.assertTrue(pkg.archive.exists(), "left in place: nothing was received")

    def test_an_older_copy_here_goes_to_a_backup(self):
        pkg = self.send()
        with self.on("desktop"):
            old = self.transcript("desktop", [{"type": "user", "cwd": self.project("desktop"),
                                               "message": {"content": "fix the parser"}}])
            before = old.read_text()
            dest, actions = transfer.unpack(self.settings, transfer.pending(self.settings)[0], self.project("desktop"))
            self.assertEqual(dest, old)
            self.assertIn("Fix the parser", dest.read_text())
            (backup,) = (self.homes["desktop"] / ".claude-accounts" / "backups" / "conversations").iterdir()
            self.assertEqual((backup / f"{SID}.jsonl").read_text(), before)
            self.assertTrue(any("earlier copy" in a for a in actions))
        self.assertFalse(pkg.path.exists())

    def test_a_project_folder_reached_through_a_symlink(self):
        """Claude Code keys conversations by the resolved folder, so that's where it has to go."""
        self.send()
        real = self.homes["desktop"] / "elsewhere" / "X"
        real.mkdir(parents=True)
        os.rmdir(self.project("desktop"))
        os.symlink(real, self.project("desktop"))
        with self.on("desktop"):
            dest, _ = transfer.unpack(self.settings, transfer.pending(self.settings)[0], self.project("desktop"))
        self.assertEqual(dest.parent.name, transfer.sanitize(str(real)))
        self.assertIn(f'"cwd": "{real}"', dest.read_text())

    def test_project_outside_projects_root_keeps_its_path(self):
        with self.on("laptop"):
            self.assertEqual(transfer.project_rel(self.settings, self.project("laptop")), "X")
            self.assertEqual(transfer.project_rel(self.settings, str(self.homes["laptop"] / "code")), ".")
            self.assertIsNone(transfer.project_rel(self.settings, "/Volumes/ext/Y"))
        pkg = transfer.Package({"session": SID, "project": None, "cwd": "/Volumes/ext/Y"}, Path("/x.json"))
        self.assertEqual(pkg.local_dir(self.settings), Path("/Volumes/ext/Y"))
        pkg.manifest["project"] = "../../etc"
        self.assertEqual(pkg.local_dir(self.settings), Path("/Volumes/ext/Y"), "never outside projects_root")

    def test_send_drops_a_half_written_last_line(self):
        with self.on("laptop"):
            t = self.laptop_conversation()
            with open(t, "a") as fh:
                fh.write('{"type": "assistant", "mess')
            pkg = transfer.pack(self.settings, SID, t, label="x", still_open=True)
        with tarfile.open(pkg.archive) as tar:
            data = tar.extractfile("conversation.jsonl").read().decode()
        self.assertEqual([json.loads(x)["type"] for x in data.splitlines()], ["user", "assistant", "ai-title"])
        self.assertTrue(pkg.manifest["open"])


class Rewriter(unittest.TestCase):
    def test_whole_paths_only_in_one_pass(self):
        fix = transfer.rewriter([("/Users/a/X", "/Users/b/code/X"), ("/Users/a", "/Users/b")])
        self.assertEqual(fix('"/Users/a/X" "/Users/a/X/f.py" "/Users/a/Y"'),
                         '"/Users/b/code/X" "/Users/b/code/X/f.py" "/Users/b/Y"')
        self.assertEqual(fix("/Users/a/X2 /Users/a/X.bak /Users/a/X-old /Users/ab /Volumes/v/Users/a"),
                         "/Users/b/X2 /Users/b/X.bak /Users/b/X-old /Users/ab /Volumes/v/Users/a")
        self.assertIsNone(transfer.rewriter([("/same", "/same")]))
        quoted = transfer.rewriter([('/Users/a/my "X"', "/Users/b/X")])
        self.assertEqual(quoted(json.dumps({"cwd": '/Users/a/my "X"'})), json.dumps({"cwd": "/Users/b/X"}))


if __name__ == "__main__":
    unittest.main()
