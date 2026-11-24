import json
import os
import time

from cas import agents
from tests.helpers import (AGENT_DONE, AGENT_INTERRUPTED, AGENT_LIMIT, AGENT_WORKING, IsolatedHome, iso,
                           write_agent)


class AgentsTest(IsolatedHome):
    def setUp(self):
        super().setUp()
        self.transcript = self.hub / "projects" / "-p" / "sess.jsonl"
        self.transcript.parent.mkdir(parents=True)
        self.transcript.write_text(json.dumps({"type": "user", "message": {"content": "go"}}) + "\n")

    def test_states_and_which_agents_a_handoff_continues(self):
        now = time.time()
        write_agent(self.transcript, "done", AGENT_DONE, True, now - 30)
        write_agent(self.transcript, "limit", AGENT_LIMIT, True, now - 20)  # the limit "finishes" an agent
        write_agent(self.transcript, "closed", AGENT_INTERRUPTED, False, now - 10)
        write_agent(self.transcript, "busy", AGENT_WORKING, False, now - 5)
        write_agent(self.transcript, "child", AGENT_WORKING, False, now - 5, depth=2)
        write_agent(self.transcript, "gone", AGENT_WORKING, False, now - 2 * agents.RUNNING_QUIET_S)
        write_agent(self.transcript, "earlier", AGENT_LIMIT, True, now - 3 * 3600)
        found = {a.id: a for a in agents.session_agents(self.transcript)}
        self.assertEqual({k: a.state() for k, a in found.items()},
                         {"done": "done", "limit": "limit", "closed": "interrupted", "busy": "running",
                          "child": "running", "gone": "running", "earlier": "limit"})
        self.assertEqual(found["limit"].name(), "task limit")

        # open session: at work = unfinished and written to lately (an agent's own agents too)
        self.assertEqual(sorted(a.id for a in agents.running(found.values(), now)), ["busy", "child"])
        # closed session: what it stopped since it started, launched by the conversation itself
        cut = agents.cut_off(found.values(), now - 3600)
        self.assertEqual(sorted(a.id for a in cut), ["busy", "closed", "gone", "limit"])

        prompt = agents.resume_prompt(cut, "work")
        self.assertIn("now runs on the work account", prompt)
        for a in cut:
            self.assertIn(f"(agent id {a.id})", prompt)
            self.assertIn(str(a.transcript), prompt)
        self.assertIn("task limit (agent id limit): it hit the usage limit", prompt)
        self.assertIn("task closed (agent id closed): the session closed", prompt)
        self.assertIn("SendMessage to its agent id", prompt)

    def test_no_agents(self):
        self.assertEqual(agents.session_agents(self.transcript), [])
        self.assertEqual(agents.session_agents(self.hub / "projects" / "-p" / "missing.jsonl"), [])

    def test_limit_errors(self):
        # Regression: the 5-hour message itself didn't match ("hit your 5-hour limit")
        five = {"type": "assistant", "isApiErrorMessage": True,
                "message": {"content": [{"type": "text", "text": "You've hit your 5-hour limit · resets 6:10pm"}]}}
        self.assertTrue(agents.limit_error(five))
        self.assertTrue(agents.limit_error(AGENT_LIMIT))
        self.assertTrue(agents.limit_error(dict(five, message={"content": "x"}, error="rate_limit")))
        self.assertFalse(agents.limit_error({"type": "user", "message": {"content": "I hit your 5-hour limit?"}}))
        self.assertFalse(agents.limit_error({"type": "assistant", "isApiErrorMessage": True,
                                             "message": {"content": "API Error: 500"}}))

    def test_last_message_past_a_long_final_entry(self):
        now = time.time()
        path = write_agent(self.transcript, "big", AGENT_WORKING, False, now)
        huge = {"type": "user", "timestamp": iso(now), "message": {"content": [
            {"type": "tool_result", "content": "x" * 200_000}]}}
        with open(path, "a") as fh:
            fh.write(json.dumps(huge) + "\n" + json.dumps({"type": "progress"}) + "\n")
        a = agents.session_agents(self.transcript)[0]
        self.assertEqual(a.last()["message"]["content"][0]["type"], "tool_result")
        self.assertEqual(a.state(), "running")
        self.assertAlmostEqual(a.last_at(), now, delta=1)
        os.utime(path, (now - 50, now - 50))
        self.assertAlmostEqual(agents.last_at(path), now, delta=1, msg="the message's time, not the file's")
