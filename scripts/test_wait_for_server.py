#!/usr/bin/env python3
"""wait_for_server's three states, asserted rather than assumed.

The distinction under test is between a server that is ABSENT and one
that has NOT BOUND YET. They look identical to a single connection
attempt -- both are ECONNREFUSED -- and the correct reading depends
entirely on whether the caller started a server itself.

Reading them the same way is what made the containers stage fail on a
hosted runner while passing locally: `entrypoint.sh` runs
`opencode models --refresh` before `serve`, so the port appears late,
and how late depends on how cold the machine's caches are. A check
written where that warm-up is fast encodes that speed as an assumption.

No sockets are opened here; `probe` is substituted so each state can be
produced on demand and the timing stays deterministic.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import e2e_session_probe as p  # noqa: E402


class _ScriptedProbe:
    """Returns a queued state per call, then repeats the last one."""

    def __init__(self, states):
        self.states = list(states)
        self.calls = 0

    def __call__(self, candidate, timeout):
        self.calls += 1
        if self.states:
            return self.states.pop(0)
        return "absent"


class WaitForServerTests(unittest.TestCase):
    def setUp(self):
        self._probe = p.probe
        self._interval = p.BIND_POLL_INTERVAL_S
        p.BIND_POLL_INTERVAL_S = 0  # keep the suite fast; behaviour is unchanged
        self.addCleanup(self._restore)

    def _restore(self):
        p.probe = self._probe
        p.BIND_POLL_INTERVAL_S = self._interval

    def test_absent_returns_immediately_when_no_server_was_started(self):
        """Without expect_server, refused means absent and must not wait.

        This is the whole reason `make client` skips in seconds on a
        machine with no server rather than blocking for the deadline.
        """
        p.probe = _ScriptedProbe(["absent"])
        self.assertEqual(p.wait_for_server(["http://x:1"], ready_timeout=30), "")
        self.assertEqual(p.probe.calls, 1, "must not retry when absence is the answer")

    def test_absent_is_retried_when_the_caller_started_a_server(self):
        """With expect_server, refused means NOT BOUND YET.

        The regression this pins: the containers stage starts the stack
        and then probes, so a not-yet-bound port is expected, not a
        failure. Reported as 'no instance answered' on the first hosted
        run while passing locally.
        """
        p.probe = _ScriptedProbe(["absent", "absent", "absent", "up"])
        self.assertEqual(
            p.wait_for_server(["http://x:1"], ready_timeout=30, expect_server=True),
            "http://x:1",
        )
        self.assertGreater(p.probe.calls, 1, "must keep sweeping until the port binds")

    def test_expect_server_gives_up_at_the_deadline(self):
        """Waiting is bounded: a server that never binds still fails."""
        p.probe = _ScriptedProbe(["absent"])
        self.assertEqual(
            p.wait_for_server(["http://x:1"], ready_timeout=0, expect_server=True), "")

    def test_listening_but_slow_is_waited_on_without_expect_server(self):
        """'slow' already meant wait, and still does. Guards the case
        this change could plausibly have broken."""
        p.probe = _ScriptedProbe(["slow", "up"])
        self.assertEqual(p.wait_for_server(["http://x:1"], ready_timeout=30), "http://x:1")

    def test_up_on_the_first_sweep_returns_without_waiting(self):
        p.probe = _ScriptedProbe(["up"])
        self.assertEqual(p.wait_for_server(["http://x:1"], ready_timeout=30), "http://x:1")
        self.assertEqual(p.probe.calls, 1)

    def test_a_later_candidate_is_reached_when_the_first_is_absent(self):
        p.probe = _ScriptedProbe(["absent", "up"])
        self.assertEqual(
            p.wait_for_server(["http://a:1", "http://b:2"], ready_timeout=30),
            "http://b:2",
        )


class RequireServerIsWiredThroughTests(unittest.TestCase):
    """The flag must reach wait_for_server, not just change the exit code.

    Added after a mutation check: setting the call site back to
    `expect_server=False` left every test above green, because they all
    call wait_for_server directly. A fix nothing asserts on can be
    reverted silently, so the WIRING needs its own assertion.
    """

    def setUp(self):
        self._wait = p.wait_for_server
        self._argv = sys.argv[:]
        self.seen = {}

        def fake_wait(candidates, ready_timeout, expect_server=False):
            self.seen["expect_server"] = expect_server
            return ""          # absent, so main returns without a session

        p.wait_for_server = fake_wait
        self.addCleanup(self._restore)

    def _restore(self):
        p.wait_for_server = self._wait
        sys.argv = self._argv

    def test_require_server_reaches_wait_for_server(self):
        sys.argv = ["e2e_session_probe.py", "--require-server",
                    "--base-url", "http://x:1"]
        self.assertEqual(p.main(), 1, "absence must be a failure for this caller")
        self.assertIs(self.seen["expect_server"], True)

    def test_without_the_flag_absence_stays_a_skip(self):
        sys.argv = ["e2e_session_probe.py", "--base-url", "http://x:1"]
        self.assertEqual(p.main(), p.SKIP)
        self.assertIs(self.seen["expect_server"], False)


if __name__ == "__main__":
    unittest.main()
