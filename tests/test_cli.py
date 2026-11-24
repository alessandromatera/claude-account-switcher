import io
import json
import os
import plistlib
import subprocess
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

from cas import __version__, cli, current, launchd, planner, resets, shared, statusline, usage
from cas.config import Config
from tests.helpers import AGENT_INTERRUPTED, AGENT_LIMIT, AGENT_WORKING, IsolatedHome, write_agent


def run_cli(*argv):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = cli.main(list(argv))
    return rc, buf.getvalue()


class CliTest(IsolatedHome):
    def add_profile(self, name="work", email="work@example.com", uuid="acct-work"):
        rc, _ = run_cli("add", name, "--no-login")
        self.assertEqual(rc, 0)
        p = Config.load().get(name)
        data = self.read_json(p.global_json)
        data["oauthAccount"] = {"accountUuid": uuid, "emailAddress": email, "organizationType": "claude_max"}
        self.write_json(p.global_json, data)
        return p

    def test_status_shows_whats_left_and_resets(self):
        p = self.add_profile()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "statusline", usage.Window(95, now + 3600),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(20, now + 7200),
                                                      usage.Window(10, now + 86400), "acct-work"))
        resets.record("default", resets.parse_status({"eligible": True, "grants": [
            {"id": "g1", "resets_left": 2, "clears": ["five_hour"], "type": "session"}]}, now))
        rc, out = run_cli("status", "--no-live", "--plain")
        self.assertEqual(rc, 0)
        self.assertIn("5-HOUR LEFT", out)
        self.assertIn("5%", out)       # default: 95% used -> 5% left
        self.assertIn("80%", out)      # work
        self.assertIn("Next up: work", out)
        self.assertIn("limit reset available → cas reset default", out)

        rc, out = run_cli("status", "--no-live", "--json")
        data = json.loads(out)
        self.assertEqual(data["next"], "work")
        self.assertEqual(data["profiles"][0]["limit_resets_left"], 2)

    def test_pick_and_env(self):
        self.add_profile()
        rc, out = run_cli("pick")
        self.assertEqual(rc, 0)
        self.assertIn(out.strip(), {"default", "work"})
        rc, out = run_cli("env", "work")
        self.assertIn("CLAUDE_CONFIG_DIR=", out)
        rc, out = run_cli("env", "default")
        self.assertIn("unset CLAUDE_CONFIG_DIR", out)
        rc, _ = run_cli("env", "nope")
        self.assertEqual(rc, 1)

    def test_statusline_records_usage_and_session(self):
        p = self.add_profile()
        cfg = Config.load()
        payload = {"session_id": "sess-1", "workspace": {"current_dir": "/proj"},
                   "rate_limits": {"five_hour": {"used_percentage": 42, "resets_at": int(time.time()) + 3600},
                                   "seven_day": {"used_percentage": 24, "resets_at": int(time.time()) + 86400}}}
        out = statusline.main(json.dumps(payload), cfg, p.env({}))
        self.assertIn("work", out)
        self.assertIn("58% left", out)
        st = usage.load_state("work")
        self.assertEqual(st["last_session"]["id"], "sess-1")
        self.assertEqual(st["usage"]["five_hour"]["pct"], 42)
        # the other accounts' usage is kept current during the session, not this one's (it comes on stdin)
        self.assertEqual(statusline.refresh_others.call_args[0][1].name, "work")
        # a number that stayed old anyway (e.g. the fetch failed) says so
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now - 1800, "live", usage.Window(8, now + 3600),
                                                         usage.Window(50, now + 86400), "acct-hub"))
        self.assertIn("next: default 92% (30m old)", statusline.render(cfg, p, now, color=False))
        # garbage input never raises
        self.assertIn("default", statusline.main("not json", cfg, {}))

    def test_statusline_suggests_handoff_at_limit(self):
        self.add_profile()
        cfg = Config.load()
        now = time.time()
        payload = {"rate_limits": {"five_hour": {"used_percentage": 100, "resets_at": int(now) + 3600}}}
        out = statusline.main(json.dumps(payload), cfg, {})
        self.assertIn("cas handoff", out)

    def test_statusline_flags_a_session_started_outside_cas(self):
        self.add_profile()
        cfg = Config.load()
        cli.set_global(last_profile="work")  # cas is on work; a stale terminal ran the claude binary
        out = statusline.main("{}", cfg, {"SHELL": "/bin/zsh"})
        self.assertIn("opened outside cas, which is on work", out)
        self.assertIn("exit, then: cas init && source ~/.zshrc && claude -c", out)  # no shell function yet
        self.assertNotIn("next:", out)

        (self.home / ".zshrc").write_text(cli.SHELL_BLOCK)
        self.assertIn("then: source ~/.zshrc && claude -c", statusline.main("{}", cfg, {"SHELL": "/bin/zsh"}))
        # no message sent yet: nothing to continue
        fresh = json.dumps({"session_id": "s1", "transcript_path": str(self.home / "none.jsonl")})
        self.assertIn("source ~/.zshrc && claude\x1b", statusline.main(fresh, cfg, {"SHELL": "/bin/zsh"}))

        # started by cas, on any account, or on an account chosen with CLAUDE_CONFIG_DIR: no warning
        self.assertNotIn("outside cas", statusline.main("{}", cfg, cfg.hub().env({})))
        self.assertNotIn("outside cas", statusline.main("{}", cfg, cfg.get("work").env({})))
        cli.set_global(last_profile=cfg.hub().name)
        self.assertNotIn("outside cas", statusline.main("{}", cfg, {}))

    def test_latest_session_and_carry_flags(self):
        cfg = Config.load()
        d = self.hub / "projects" / cli.sanitize("/my/proj")
        d.mkdir(parents=True)
        (d / "old.jsonl").write_text("{}")
        os.utime(d / "old.jsonl", (time.time() - 100, time.time() - 100))
        (d / "new.jsonl").write_text("{}")
        self.assertEqual(cli.latest_session(cfg, ["/my/proj"]), "new")
        self.assertIsNone(cli.latest_session(cfg, ["/my/proj"], since=time.time() + 10))
        args = ["--model", "opus", "fix it", "--add-dir", "a", "b", "-c", "--dangerously-skip-permissions",
                "--resume", "x"]
        self.assertEqual(cli.carry_flags(args),
                         ["--model", "opus", "--add-dir", "a", "b", "--dangerously-skip-permissions"])

    def test_transcript_limit_detection_ignores_conversation_text(self):
        d = self.hub / "projects" / cli.sanitize("/p")
        d.mkdir(parents=True)
        lines = [
            {"type": "user", "message": {"content": "what happens when I hit your session limit?"}},
            {"type": "assistant", "isApiErrorMessage": True, "message": {"content": "You've hit your session limit"}},
        ]
        (d / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines[:1]))
        self.assertFalse(cli.transcript_says_limit("s", ["/p"], 0))
        (d / "s.jsonl").write_text("\n".join(json.dumps(x) for x in lines))
        self.assertTrue(cli.transcript_says_limit("s", ["/p"], 0))

    def test_session_from_args(self):
        self.assertEqual(cli.session_from_args(["--model", "x"]), (None, False))
        self.assertEqual(cli.session_from_args(["--resume", "abc"]), ("abc", True))
        self.assertEqual(cli.session_from_args(["--session-id=u1"]), ("u1", True))
        self.assertEqual(cli.session_from_args(["-c"]), (None, True))
        self.assertEqual(cli.session_from_args(["--resume", "abc", "--fork-session"]), (None, True))

    def test_handoff_picks_among_recent_conversations(self):
        cfg = Config.load()
        d = self.hub / "projects" / cli.sanitize("/p")
        d.mkdir(parents=True)
        for i, (sid, text) in enumerate([("old", "first task"), ("new", "second task")]):
            (d / f"{sid}.jsonl").write_text(json.dumps({"type": "user", "message": {"content": text}}) + "\n")
            os.utime(d / f"{sid}.jsonl", (time.time() - 600 + i * 300,) * 2)
        style = cli.Style(False)
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.choose_session(cfg, ["/p"], style), "new")
        self.assertIn("2 recent conversations", out.getvalue())
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="2"), \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.choose_session(cfg, ["/p"], style), "old")
        self.assertIn("first task", out.getvalue())

    def test_handoff_moves_from_the_account_the_session_ran_on(self):
        """Regression: the source account comes from the status line, not from the last `cas run`."""
        self.add_profile()
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(100, now + 3600),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(8, now + 7200),
                                                      usage.Window(30, now + 86400), "acct-work"))
        statusline.main(json.dumps({"session_id": "s-hub", "workspace": {"current_dir": "/p"}}), cfg, {})
        statusline.main(json.dumps({"session_id": "s-work", "workspace": {"current_dir": "/q"}}), cfg,
                        cfg.get("work").env({}))
        statusline.main(json.dumps({"session_id": "s-work2", "workspace": {"current_dir": "/q"}}), cfg,
                        cfg.get("work").env({}))
        cli.set_global(last_profile="work")  # the misleading hint the old code used
        self.assertEqual(cli.session_profile(cfg, "s-hub").name, "default")
        self.assertEqual(cli.session_profile(cfg, "s-work").name, "work", "older sessions are remembered too")
        args = cli.build_parser().parse_args(["handoff", "--session", "s-hub"])
        with mock.patch.object(cli, "run_loop", return_value=0) as rl, redirect_stdout(io.StringIO()) as out:
            cli.cmd_handoff(args)
        self.assertEqual(rl.call_args[0][1], "work")
        self.assertIn("handoff default → work", out.getvalue())
        # no other account has room, but the source does: offer to stay, no false "limit reached"
        args = cli.build_parser().parse_args(["handoff", "--session", "s-work"])
        with mock.patch.object(cli, "run_loop", return_value=0) as rl, mock.patch.object(cli, "confirm",
                                                                                          return_value=True), \
                redirect_stdout(io.StringIO()) as out:
            cli.cmd_handoff(args)
        self.assertEqual(rl.call_args[0][1], "work")
        self.assertIn("No other account has room", out.getvalue())
        self.assertNotIn("reached its", out.getvalue())

    def test_split_run_args(self):
        self.add_profile()
        cfg = Config.load()
        self.assertEqual(cli.split_run_args(["-y", "work", "-c"], cfg), ({"mode": "auto"}, "work", ["-c"]))
        self.assertEqual(cli.split_run_args(["fix the bug"], cfg)[1:], ("auto", ["fix the bug"]))
        self.assertEqual(cli.split_run_args(["--", "work"], cfg)[1:], ("auto", ["work"]))

    def enable_limit_reset_command(self):
        data = self.read_json(self.hub_json)
        data["cachedGrowthBookFeatures"] = {"tengu_cedar_ember": {"enabled": True}}
        self.write_json(self.hub_json, data)

    def test_decide_offers_reset_only_with_permission(self):
        self.add_profile()
        self.enable_limit_reset_command()
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(100, now + 7200),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(99, now + 3600),
                                                      usage.Window(30, now + 86400), "acct-work"))
        resets.record("default", resets.parse_status({"eligible": True, "grants": [
            {"id": "g1", "resets_left": 1, "clears": ["five_hour"], "type": "session"}]}, now))
        rows = cli.gather(cfg, live=False)
        style = cli.Style(False)
        # non-interactive: never uses a reset on its own
        with mock.patch("sys.stdin.isatty", return_value=False), redirect_stdout(io.StringIO()) as buf:
            a = cli.decide(cfg, rows, style, cfg.hub(), "auto")
        self.assertEqual(a.kind, "wait")
        self.assertIn("/limit-reset", buf.getvalue())
        self.assertIn("What's left", buf.getvalue())
        # interactive: reset is recommended (no other account, 2h wait) and needs an explicit choice
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value=""), \
                redirect_stdout(io.StringIO()) as buf:
            a = cli.decide(cfg, rows, style, cfg.hub(), "ask")
        self.assertEqual((a.kind, a.grant.id), ("reset", "g1"))
        self.assertIn("(recommended)", buf.getvalue())
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="2"), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.decide(cfg, rows, style, cfg.hub(), "ask").kind, "wait")

    def test_decide_unknown_resets_offers_a_check_but_recommends_waiting(self):
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(100, now + 7200),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        resets.record("default", resets.parse_status({"eligible": False, "ineligible_reason": "surface"}, now))
        rows = cli.gather(cfg, live=False)
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value=""), \
                redirect_stdout(io.StringIO()) as buf:
            a = cli.decide(cfg, rows, cli.Style(False), cfg.hub(), "ask")
        self.assertEqual(a.kind, "wait")
        self.assertIn("check for a limit reset on default", buf.getvalue())
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value="1"), \
                redirect_stdout(io.StringIO()):
            a = cli.decide(cfg, rows, cli.Style(False), cfg.hub(), "ask")
        self.assertEqual((a.kind, a.grant), ("reset", None))

    def test_decide_prefers_free_switch_over_reset(self):
        self.add_profile()
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(100, now + 7200),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        resets.record("default", resets.parse_status({"eligible": True, "grants": [
            {"id": "g1", "resets_left": 1, "clears": ["five_hour"]}]}, now))
        rows = cli.gather(cfg, live=False)
        with mock.patch("sys.stdin.isatty", return_value=True), mock.patch("builtins.input", return_value=""), \
                redirect_stdout(io.StringIO()):
            a = cli.decide(cfg, rows, cli.Style(False), cfg.hub(), "ask")
        self.assertEqual((a.kind, a.profile.name), ("switch", "work"))

    def fake_run(self, cfg, mode, answers=None, agent=False):
        """Run run_loop with a fake `claude` that hits the limit whenever it runs on 'default'.

        agent: the limit also stops a subagent of the conversation."""
        cfg.settings["prime_others_on_start"] = False  # never spawn real background pings from tests
        proj = self.home / "proj"
        proj.mkdir()
        log = self.home / "calls.jsonl"
        fake = self.home / "fake-claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys, pathlib, datetime\n"
            "log = pathlib.Path(os.environ['FAKE_LOG'])\n"
            "with log.open('a') as f: f.write(json.dumps({'args': sys.argv[1:], "
            "'cfg': os.environ.get('CLAUDE_CONFIG_DIR'), 'profile': os.environ.get('CAS_PROFILE')}) + '\\n')\n"
            "a = sys.argv[1:]\n"
            "sid = a[a.index('--session-id') + 1] if '--session-id' in a else a[a.index('--resume') + 1]\n"
            "if os.environ.get('CAS_PROFILE') == 'default':\n"
            "    d = pathlib.Path(os.environ['FAKE_PROJECTS']); d.mkdir(parents=True, exist_ok=True)\n"
            "    ts = datetime.datetime.now(datetime.timezone.utc).isoformat()\n"
            "    (d / (sid + '.jsonl')).write_text(json.dumps({'type': 'assistant', 'isApiErrorMessage': True, "
            "'timestamp': ts, 'message': {'content': \"You've hit your session limit\"}}) + '\\n')\n"
            "    if os.environ.get('FAKE_AGENT'):\n"
            "        a = d / sid / 'subagents'; a.mkdir(parents=True, exist_ok=True)\n"
            "        (a / 'agent-a1.meta.json').write_text(json.dumps({'description': 'survey', 'finished': True}))\n"
            "        (a / 'agent-a1.jsonl').write_text(json.dumps({'type': 'assistant', 'isApiErrorMessage': True, "
            "'error': 'rate_limit', 'timestamp': ts, 'message': {'content': \"You've hit your 5-hour limit\"}}))\n"
        )
        fake.chmod(0o755)
        cwd = os.getcwd()
        os.environ.update(FAKE_LOG=str(log), PWD=str(proj),
                          FAKE_PROJECTS=str(self.hub / "projects" / cli.sanitize(os.path.realpath(proj))))
        if agent:
            os.environ["FAKE_AGENT"] = "1"
        answers = list(answers or [])
        try:
            os.chdir(proj)
            with mock.patch.object(cli, "find_claude", return_value=str(fake)), \
                    mock.patch.object(cli, "refresh_in_background", return_value=[]), \
                    mock.patch("sys.stdin.isatty", return_value=bool(answers)), \
                    mock.patch("builtins.input", side_effect=lambda *_: answers.pop(0)), \
                    redirect_stdout(io.StringIO()) as out:
                rc = cli.run_loop(cfg, "default", ["--model", "opus", "hello"], mode, cli.Style(False))
        finally:
            os.chdir(cwd)
            for k in ("FAKE_LOG", "FAKE_PROJECTS", "FAKE_AGENT"):
                os.environ.pop(k, None)
        self.assertEqual(rc, 0)
        return [json.loads(x) for x in log.read_text().splitlines()], out

    def test_run_loop_hands_conversation_to_next_account(self):
        """End to end with a fake `claude`: account A hits its limit, the conversation resumes on B."""
        self.add_profile()
        cfg = Config.load()
        cfg.settings["live_usage"] = "never"
        calls, out = self.fake_run(cfg, "auto")
        self.assertEqual(len(calls), 2, out.getvalue())
        self.assertEqual((calls[0]["profile"], calls[0]["cfg"], calls[0]["args"][0], calls[0]["args"][2:]),
                         ("default", None, "--session-id", ["--model", "opus", "hello"]))
        sid = calls[0]["args"][1]
        self.assertEqual(calls[1]["profile"], "work")
        self.assertEqual(calls[1]["cfg"], str(cfg.get("work").config_dir))
        self.assertEqual(calls[1]["args"], ["--model", "opus", "--resume", sid], "resumes exactly its own session")
        self.assertIn("handoff: auto → work", out.getvalue())

    def test_run_loop_has_claude_continue_the_agents_the_limit_stopped(self):
        self.add_profile()
        cfg = Config.load()
        cfg.settings["live_usage"] = "never"
        calls, out = self.fake_run(cfg, "auto", agent=True)
        self.assertEqual(len(calls), 2, out.getvalue())
        sid = calls[0]["args"][1]
        self.assertEqual(calls[1]["args"][:4], ["--model", "opus", "--resume", sid])
        prompt = calls[1]["args"][4]
        self.assertIn("survey (agent id a1): it hit the usage limit", prompt)
        self.assertIn("SendMessage", prompt)
        self.assertIn("1 agent stopped before finishing; Claude continues it on work: survey", out.getvalue())

        cfg.settings["resume_agents"] = False
        os.remove(self.home / "calls.jsonl")
        (self.home / "proj").rmdir()
        calls, _ = self.fake_run(cfg, "auto", agent=True)
        self.assertEqual(calls[1]["args"], ["--model", "opus", "--resume", calls[0]["args"][1]])

    def test_handoff_continues_the_agents_its_session_stopped(self):
        self.add_profile()
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(100, now + 3600),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(8, now + 7200),
                                                      usage.Window(30, now + 86400), "acct-work"))
        statusline.main(json.dumps({"session_id": "s1", "workspace": {"current_dir": "/p"}}), cfg, {})
        t = self.hub / "projects" / cli.sanitize("/p") / "s1.jsonl"
        t.parent.mkdir(parents=True)
        t.write_text(json.dumps({"type": "user", "timestamp": "2026-01-01T09:00:00Z",
                                 "message": {"content": "go"}}) + "\n")
        os.utime(t, (now - 600, now - 600))
        write_agent(t, "a1", AGENT_INTERRUPTED, False, now - 300, description="survey")
        write_agent(t, "a2", AGENT_WORKING, False, now - 300, depth=2)

        def handoff(*extra):
            args = cli.build_parser().parse_args(["handoff", "--session", "s1", *extra])
            with mock.patch.object(cli, "run_loop", return_value=0) as rl, redirect_stdout(io.StringIO()) as out:
                cli.cmd_handoff(args)
            return rl.call_args, out.getvalue()

        call, out = handoff()
        self.assertEqual(call[0][1:3], ("work", ["--resume", "s1"]))
        (prompt,) = call[1]["prompt"]
        self.assertIn("survey (agent id a1): the session closed", prompt)
        self.assertNotIn("a2", prompt, "an agent's own agents are its business")
        self.assertIn("Claude continues it on work: survey", out)
        call, _ = handoff("--no-agents")
        self.assertEqual(call[1]["prompt"], [])

        # an agent still writing means the session may still be open: ask first
        write_agent(t, "a1", AGENT_WORKING, False, time.time())
        args = cli.build_parser().parse_args(["handoff", "--session", "s1"])
        with mock.patch.object(cli, "confirm", return_value=False) as ask, mock.patch.object(cli, "run_loop") as rl, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.cmd_handoff(args), 1)
        self.assertIn("active less than 90s ago", ask.call_args[0][0])
        rl.assert_not_called()

    def test_statusline_warns_about_running_agents_near_the_limit(self):
        p = self.add_profile()
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(10, now + 3600),
                                                         usage.Window(30, now + 86400), "acct-hub"))
        t = self.home / "proj" / "s1.jsonl"
        t.parent.mkdir()
        t.write_text("{}\n")
        write_agent(t, "a1", AGENT_WORKING, False, now - 30)
        write_agent(t, "a2", AGENT_WORKING, False, now - 30)

        def line(pct):
            payload = {"session_id": "s1", "transcript_path": str(t),
                       "rate_limits": {"five_hour": {"used_percentage": pct, "resets_at": int(now) + 3600}}}
            return statusline.main(json.dumps(payload), cfg, p.env({}))

        self.assertIn("next: default", line(50))  # far from the limit: no warning
        out = line(80)
        self.assertIn("⚠ 2 agents running, limit near → let them finish, then exit and `cas handoff` → default", out)
        self.assertNotIn("next:", out)
        cfg.settings["agent_warning"] = 0
        self.assertNotIn("agents running", line(80))
        cfg.settings["agent_warning"] = 15

        # at the limit the agents stop too; the handoff continues them
        write_agent(t, "a2", AGENT_LIMIT, True, now)
        self.assertIn("limit: exit, then `cas handoff` → default, which continues the 2 agents", line(100))
        cfg.settings["resume_agents"] = False
        self.assertNotIn("continues", line(100))

    def test_run_loop_reset_path_asks_and_offers_once(self):
        """No other account has room: the user picks the reset; it opens /limit-reset; never offered twice."""
        self.add_profile()
        self.enable_limit_reset_command()
        cfg = Config.load()
        cfg.settings["live_usage"] = "never"
        now = time.time()
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(99, now + 3600),
                                                      usage.Window(30, now + 86400), "acct-work"))
        resets.record("default", resets.parse_status({"eligible": True, "grants": [
            {"id": "g1", "resets_left": 1, "clears": ["five_hour"], "type": "session"}]}, now))
        calls, out = self.fake_run(cfg, "auto", answers=["", ""])  # Enter = recommended, twice
        self.assertEqual(len(calls), 2, out.getvalue())
        self.assertEqual(calls[1]["profile"], "default")
        self.assertEqual(calls[1]["args"], ["--model", "opus", "--resume", calls[0]["args"][1], "/limit-reset 5-hour"])
        text = out.getvalue()
        self.assertIn("you confirm there", text)
        self.assertEqual(text.count("use 5-hour reset on default"), 1, "reset offered only once per run")

    def test_prime_others_only_pings_closed_windows_once(self):
        self.add_profile()
        rc, _ = run_cli("add", "third", "--no-login")
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now, "live", usage.Window(40, now + 3600),
                                                         usage.Window(10, now + 86400), "acct-hub"))
        with mock.patch("subprocess.Popen") as popen, redirect_stdout(io.StringIO()) as out:
            started = cli.prime_others(cfg, cfg.get("work"), cli.Style(False), now)
            # default has an open window, third isn't logged in, work is the target
            self.assertEqual(started, [])
            started = cli.prime_others(cfg, cfg.get("default"), cli.Style(False), now)
            self.assertEqual(started, ["work"])
            self.assertEqual(popen.call_args[0][0][-2:], ["ping", "work"])
            self.assertTrue(popen.call_args[1]["start_new_session"])
            self.assertEqual(cli.prime_others(cfg, cfg.get("default"), cli.Style(False), now + 60), [],
                             "not spawned twice within 10 minutes")
            cfg.settings["prime_others_on_start"] = False
            self.assertEqual(cli.prime_others(cfg, cfg.get("default"), cli.Style(False), now + 3600), [])
        self.assertIn("opening the 5-hour window of work", out.getvalue())

    def test_rename_keeps_login_dir_state_and_schedule(self):
        p = self.add_profile()
        usage.update_state("work", last_ping={"ts": 1})
        usage.update_state("default", last_ping={"ts": 2})
        cli.set_global(last_profile="work")
        entries = planner.calendar_entries([360], [1])
        with mock.patch.object(launchd, "_launchctl", return_value=mock.Mock(returncode=0)), \
                mock.patch.object(launchd, "supported", return_value=True):
            launchd.install("work", entries, "/py", "/cas", "/bin")
            rc, out = run_cli("rename", "work", "gmail.com")
            self.assertEqual(rc, 0, out)
            rc, out = run_cli("rename", "default", "example.com")
            self.assertEqual(rc, 0, out)
        cfg = Config.load()
        self.assertEqual(list(cfg.profiles), ["example.com", "gmail.com"])
        self.assertTrue(cfg.get("example.com").is_hub)
        self.assertEqual(cfg.get("gmail.com").config_dir, p.config_dir, "dir unchanged, so the login survives")
        self.assertEqual(cfg.get("gmail.com").email, "work@example.com")
        self.assertEqual(usage.load_state("gmail.com")["last_ping"], {"ts": 1})
        self.assertEqual(usage.load_state("example.com")["last_ping"], {"ts": 2})
        self.assertEqual(cli.global_state()["last_profile"], "gmail.com")
        self.assertEqual(list(launchd.installed()), ["gmail.com"])
        with open(launchd.plist_path("gmail.com"), "rb") as f:
            import plistlib
            self.assertEqual(plistlib.load(f)["ProgramArguments"][3], "gmail.com")
        self.assertEqual(cfg.current({}).name, "example.com")
        self.assertEqual(cfg.current(cfg.get("gmail.com").env({})).name, "gmail.com")
        # bad names are refused
        self.assertEqual(run_cli("rename", "gmail.com", "bad..name")[0], 1)
        self.assertEqual(run_cli("rename", "gmail.com", "auto")[0], 1)
        self.assertEqual(run_cli("rename", "gmail.com", "example.com")[0], 1)
        self.assertEqual(run_cli("add", "x.com.", "--no-login")[0], 1)

    def test_reset_on_claude_ai_when_claude_code_cant(self):
        """No /limit-reset in Claude Code: cas opens claude.ai's usage page and resumes once the user is done."""
        self.add_profile()
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(99, now + 3600),
                                                      usage.Window(30, now + 86400), "acct-work"))
        resets.set_manual(cfg.hub(), 4, "full", now + 86400 * 18)
        real_call = cli.subprocess.call
        opened = []

        def fake_call(cmd, *a, **kw):
            if cmd[0] == "open":
                opened.append(cmd[1])
                return 0
            return real_call(cmd, *a, **kw)

        with mock.patch.object(cli.subprocess, "call", side_effect=fake_call), \
                mock.patch.object(cli, "confirm", return_value=True), \
                mock.patch.object(cli, "refresh", side_effect=lambda p, s, live=None: (usage.best_snapshot(p), None)):
            calls, out = self.fake_run(cfg, "auto", answers=["", ""])
        self.assertEqual(opened, [resets.WEB_USAGE_URL])
        self.assertIn("use full reset on default", out.getvalue())
        self.assertIn("click Reset there", out.getvalue())
        self.assertEqual(calls[1]["profile"], "default")
        self.assertEqual(calls[1]["args"][-2], "--resume", "resumes the conversation, no /limit-reset")
        rc, out = run_cli("reset", "work", "--set", "2", "--expires", "2026-12-10")
        self.assertIn("2 full resets", out)

    def test_plan_and_plist(self):
        self.add_profile()
        rc, out = run_cli("plan", "--start", "08:00", "--end", "18:00")
        self.assertEqual(rc, 0)
        self.assertIn("pings 05:00", out)
        pl = planner.plan(["work"], 480, 1080)
        plist = launchd.build_plist("work", planner.calendar_entries(pl["work"], [1, 2]), "/py", "/cas", "/bin")
        self.assertEqual(plist["ProgramArguments"], ["/py", "/cas", "ping", "work", "--scheduled"])
        self.assertIn({"Weekday": 1, "Hour": 5, "Minute": 0}, plist["StartCalendarInterval"])

    def test_init_installs_statusline_and_keeps_previous_one(self):
        self.write_json(self.hub / "settings.json", {"model": "opus", "statusLine": {"type": "command",
                                                                                     "command": "my-line"}})
        with mock.patch.object(cli, "find_claude", return_value="/bin/echo"):
            rc, out = run_cli("init")
        self.assertEqual(rc, 0)
        st = self.read_json(self.hub / "settings.json")
        self.assertTrue(st["statusLine"]["command"].endswith("bin/cas statusline"))
        self.assertEqual(st["model"], "opus")
        self.assertEqual(Config.load().settings["statusline_chain"], "my-line")
        cmd = (self.hub / "commands" / "accounts.md").read_text()
        self.assertIn("status --plain", cmd)
        self.assertTrue((self.home / ".local" / "bin" / "cas").is_symlink())
        rc, out = run_cli("uninstall")
        self.assertEqual(self.read_json(self.hub / "settings.json")["statusLine"]["command"], "my-line")
        self.assertFalse((self.hub / "commands" / "accounts.md").exists())

    def fake_exec(self, *argv, env=None):
        """Run `cas claude ARGV`; returns (args claude got, CLAUDE_CONFIG_DIR, CAS_PROFILE)."""
        got = {}

        def execve(path, args, e):
            got.update(args=args[1:], cfg=e.get("CLAUDE_CONFIG_DIR"), profile=e.get("CAS_PROFILE"))
            raise SystemExit(0)

        with mock.patch.dict(os.environ, env or {}), mock.patch.object(cli, "find_claude", return_value="/bin/claude"), \
                mock.patch.object(cli.os, "execve", side_effect=execve):
            with self.assertRaises(SystemExit):
                run_cli("claude", *argv)
        return got["args"], got["cfg"], got["profile"]

    def test_fresh_claude_starts_on_the_current_account(self):
        p = self.add_profile()
        # nothing run through cas yet: the hub, as before
        self.assertEqual(self.fake_exec("hi"), (["hi"], None, "default"))
        cli.set_global(last_profile="work")
        self.assertEqual(self.fake_exec("-c"), (["-c"], str(p.config_dir), "work"))
        self.assertTrue((p.dir / "projects").is_symlink(), "the profile is prepared like `cas run` does")
        rc, out = run_cli("env")
        self.assertIn(f"CLAUDE_CONFIG_DIR={p.config_dir}", out)
        self.assertEqual(json.loads(run_cli("status", "--no-live", "--json")[1])["current"], "work")
        # a profile's default model applies to sessions, not to subcommands
        run_cli("set", "work", "--model", "sonnet")
        self.assertEqual(self.fake_exec("hi")[0], ["--model", "sonnet", "hi"])
        self.assertEqual(self.fake_exec("agents"), (["agents"], str(p.config_dir), "work"))
        # setup cas spreads from ~/.claude stays there
        self.assertEqual(self.fake_exec("mcp", "add", "x"), (["mcp", "add", "x"], None, None))
        # an account picked already (cas env, or a nested claude inside a session) is kept
        self.assertEqual(self.fake_exec("hi", env={"CAS_PROFILE": "default"}), (["hi"], None, "default"))
        self.assertEqual(self.fake_exec("-p", "x", env={"CLAUDECODE": "1"})[1:], (None, None))
        # the current account disabled or gone: back to the hub
        run_cli("set", "work", "--disable")
        self.assertEqual(self.fake_exec("hi")[1:], (None, "default"))

    def test_shell_hook_install_update_and_remove(self):
        zshrc = self.home / ".zshrc"
        zshrc.write_text("export A=1\n")
        with mock.patch.object(cli, "find_claude", return_value="/bin/echo"):
            rc, out = run_cli("init")
        self.assertEqual(rc, 0)
        text = zshrc.read_text()
        self.assertTrue(text.startswith("export A=1\n\n" + cli.SHELL_BEGIN))
        self.assertIn('command cas claude "$@"', text)
        self.assertTrue(cli.shell_hook_installed())
        # idempotent, and an outdated block is replaced in place
        zshrc.write_text(text.replace("command cas claude", "old") + "export B=2\n")
        cli.install_shell_hook()
        cli.install_shell_hook()
        self.assertEqual(zshrc.read_text(), text + "export B=2\n")
        # a dotfiles symlink keeps pointing at its target
        real = self.home / "dotfiles" / "bashrc"
        real.parent.mkdir()
        real.write_text("x\n")
        os.symlink(real, self.home / ".bashrc")
        cli.install_shell_hook()
        self.assertTrue((self.home / ".bashrc").is_symlink())
        self.assertIn(cli.SHELL_BEGIN, real.read_text())
        run_cli("uninstall")
        self.assertEqual(zshrc.read_text(), "export A=1\nexport B=2\n")
        self.assertEqual(real.read_text(), "x\n")
        self.assertFalse(cli.shell_hook_installed())

    def test_fresh_sessions_follow_the_account_chosen_on_another_machine(self):
        """Limits belong to the account: after a switch on one Mac, the other's fresh sessions follow."""
        self.add_profile()  # work = acct-work
        store = self.home / "store"
        cfg = Config.load()
        cfg.settings["shared_store"] = str(store)
        cfg.save()
        cfg = Config.load()
        now = time.time()
        theirs = store / "current" / "MBP.json"
        with mock.patch.object(shared, "host", return_value="studio"):
            current.set_current(cfg, cfg.hub(), now)
            self.assertEqual(self.read_json(store / "current" / "studio.json"), {"account": "acct-hub", "at": now})
            self.assertEqual(cli.active_profile(cfg).name, cfg.hub().name)

            self.write_json(theirs, {"account": "acct-work", "at": now + 60})  # the MBP moved to work
            p, where = current.get_current(cfg)
            self.assertEqual((p.name, where), ("work", "MBP"))
            self.assertEqual(self.fake_exec("hi")[2], "work")
            with mock.patch.object(cli, "shell_hook_installed", return_value=True):
                self.assertIn("starts on work, the account cas last used on MBP", run_cli("status", "--no-live")[1])

            self.write_json(theirs, {"account": "acct-nobody-here", "at": now + 90})  # no profile for it here
            self.assertEqual(cli.active_profile(cfg).name, cfg.hub().name)
            self.write_json(theirs, {"account": "acct-work", "at": now + 60})
            current.set_current(cfg, cfg.hub(), now + 120)  # a newer choice here wins again
            self.assertEqual(current.get_current(cfg)[1], None)
            self.assertEqual(cli.active_profile(cfg).name, cfg.hub().name)

            rc, out = run_cli("use", "work")  # choose without starting a session
            self.assertEqual(rc, 0, out)
            self.assertIn("starts on work on all your machines", out)
            self.assertEqual(self.read_json(store / "current" / "studio.json")["account"], "acct-work")
            self.assertEqual(cli.active_profile(Config.load()).name, "work")
            run_cli("add", "third", "--no-login")
            self.assertNotEqual(cli.main(["use", "third"]), 0, "not logged in: refused")

    def test_install_runs_cas_from_a_copy_and_points_everything_at_it(self):
        """Edits to the source folder (kept in Dropbox) must not change the cas in use until `cas install`."""
        src = cli.SCRIPT.parent.parent
        self.write_json(self.hub / "settings.json", {"statusLine": {
            "type": "command", "command": f"/usr/bin/python3 {cli.SCRIPT} statusline"}})
        link = self.home / ".local" / "bin" / "cas"
        link.parent.mkdir(parents=True)
        os.symlink(cli.SCRIPT, link)
        agents = self.home / "Library" / "LaunchAgents"
        agents.mkdir(parents=True)
        with open(agents / f"{launchd.LABEL_PREFIX}work.plist", "wb") as fh:
            plistlib.dump(launchd.build_plist("work", [{"Hour": 6, "Minute": 0}], "/py", str(cli.SCRIPT), "/bin"), fh)
        dest = cli.install_dir()
        script = dest / "bin" / "cas"
        with mock.patch.object(launchd, "supported", return_value=True), \
                mock.patch.object(launchd, "_launchctl", return_value=mock.Mock(returncode=0)):
            rc, out = run_cli("install")
            self.assertEqual(rc, 0, out)
            script = script.resolve()
            self.assertEqual(cli.install_differs(src, dest), [])
            self.assertEqual(self.read_json(dest / cli.INSTALL_MARK)["source"], str(src))
            self.assertEqual(os.path.realpath(link), str(script))
            self.assertIn(str(script), self.read_json(self.hub / "settings.json")["statusLine"]["command"])
            with open(agents / f"{launchd.LABEL_PREFIX}work.plist", "rb") as fh:
                self.assertEqual(plistlib.load(fh)["ProgramArguments"][1], str(script))
            self.assertFalse((self.hub / "commands" / "accounts.md").exists(), "not set up before, so not added")
            r = subprocess.run([sys.executable, str(script), "--version"], capture_output=True, text=True)
            self.assertEqual(r.stdout.strip(), f"cas {__version__}", r.stderr)

            (dest / "cas" / "gone.py").write_text("")  # a module since removed from the source
            self.assertEqual(run_cli("install")[0], 0)
            self.assertFalse((dest / "cas" / "gone.py").exists())

            # `cas install` from the installed copy installs again from the source, with the source's installer
            with mock.patch.object(cli, "SCRIPT", script), mock.patch("subprocess.call", return_value=0) as call:
                self.assertEqual(run_cli("install")[0], 0)
            self.assertEqual(call.call_args[0][0][1:], [str(src / "bin" / "cas"), "install"])

            run_cli("uninstall")
        self.assertFalse(link.is_symlink() or link.exists())
        self.assertFalse(dest.exists())

    def test_ping_skips_open_window_and_records_new_one(self):
        self.add_profile()
        cfg = Config.load()
        p = cfg.get("work")
        now = time.time()
        usage.record_snapshot("work", usage.Snapshot(now, "live", usage.Window(10, now + 7200),
                                                      usage.Window(5, now + 86400), "acct-work"))
        with redirect_stdout(io.StringIO()) as buf:
            self.assertTrue(cli.do_ping(cfg, p, None, False, False))
        self.assertIn("already open", buf.getvalue())

        usage.save_state("work", {})
        fake = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with mock.patch.object(cli, "find_claude", return_value="/bin/claude"), \
                mock.patch("subprocess.run", return_value=fake) as run, \
                mock.patch.object(cli, "refresh", side_effect=lambda p, s, live=None: (usage.best_snapshot(p), None)), \
                mock.patch.object(cli.time, "sleep") as sleep, \
                redirect_stdout(io.StringIO()):
            self.assertTrue(cli.do_ping(cfg, p, None, False, False))
        argv = run.call_args[0][0]
        self.assertEqual(argv[:4], ["/bin/claude", "-p", cli.PING_PROMPT, "--model"])
        self.assertEqual(run.call_args[1]["env"]["CLAUDE_CONFIG_DIR"], str(p.config_dir))
        snap = usage.best_snapshot(p)
        self.assertEqual(snap.source, "ping")
        self.assertAlmostEqual(snap.five_hour.resets_at, time.time() + 5 * 3600, delta=60)
        self.assertEqual(sleep.call_count, len(cli.PING_SETTLE_WAITS), "kept asking for the exact reset")

    def test_ping_waits_for_the_usage_endpoint_to_count_it(self):
        """Right after a ping the endpoint still shows no 5-hour window: that reading must not
        replace the window the ping opened (the status line showed `next: gmail ?`)."""
        self.add_profile()
        cfg = Config.load()
        p = cfg.get("work")
        exact = time.time() + 4 * 3600
        readings = [usage.Window(None, None), usage.Window(0, exact)]  # not counted yet, then counted
        calls = []

        def live(prof, s, live=None):
            calls.append(live)
            if not live:  # the check before pinging: nothing cached
                return usage.best_snapshot(prof), None
            w = readings.pop(0)
            usage.record_snapshot(prof.name, usage.Snapshot(time.time(), "live", w, usage.Window(50, exact + 86400),
                                                            prof.account_uuid))
            return usage.best_snapshot(prof), None

        fake = mock.Mock(returncode=0, stdout="ok\n", stderr="")
        with mock.patch.object(cli, "find_claude", return_value="/bin/claude"), \
                mock.patch("subprocess.run", return_value=fake), \
                mock.patch.object(cli, "refresh", side_effect=live), mock.patch.object(cli.time, "sleep"), \
                redirect_stdout(io.StringIO()) as out:
            self.assertTrue(cli.do_ping(cfg, p, None, False, False))
        self.assertEqual(calls, [None, True, True], "asked again after the reading that missed the window")
        snap = usage.best_snapshot(p)
        self.assertEqual((snap.source, snap.five_hour.resets_at), ("live", exact))
        self.assertNotIn("resets -", out.getvalue())

    def test_session_start_refreshes_out_of_date_usage_in_the_background(self):
        self.add_profile()
        rc, _ = run_cli("add", "third", "--no-login")
        cfg = Config.load()
        now = time.time()
        usage.record_snapshot("default", usage.Snapshot(now - 60, "statusline", usage.Window(40, now + 3600),
                                                         usage.Window(10, now + 86400), "acct-hub"))
        with mock.patch("subprocess.Popen") as popen:
            # default is fresh, third isn't logged in, work has no data at all
            self.assertEqual(cli.refresh_in_background(cfg, now=now), ["work"])
            self.assertEqual(popen.call_args[0][0][-2:], [cli.REFRESH_CMD, "work"])
            self.assertTrue(popen.call_args[1]["start_new_session"])
            self.assertEqual(cli.refresh_in_background(cfg, now=now + 30), [],
                             "the status line asks on every refresh: one fetch per live_max_age_s, even if it failed")
            self.assertEqual(cli.refresh_in_background(cfg, skip=["work"], now=now), [], "a ping refreshes it")
            self.assertEqual(cli.refresh_in_background(cfg, now=now + 3600), ["default", "work"])
            cfg.settings["live_usage"] = "never"
            self.assertEqual(cli.refresh_in_background(cfg, now=now + 3600), [])
        self.assertEqual(popen.call_count, 2)

        cfg.settings["live_usage"] = "auto"
        with mock.patch.object(cli, "refresh", return_value=(None, "token expired")) as ref, \
                redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.main([cli.REFRESH_CMD, "work", "third", "gone"]), 0)
        self.assertEqual([c[0][0].name for c in ref.call_args_list], ["work"])
        self.assertIn("work: token expired", out.getvalue())


if __name__ == "__main__":
    unittest.main()
