"""Guards that keep an unattended run honest.

Each exists because its absence cost real time during development:

* A driver that dies instantly gets restarted forever and looks identical to a
  working one from outside. That ran for an hour reporting healthy.
* Six hours of retrying an out-of-credits error, charging attempt budget against
  healthy conversations, because nothing distinguished "this work is bad" from
  "the world is broken".
"""

from __future__ import annotations

from pathlib import Path
import sys
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "golden-conversations" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))


class CircuitBreakerTests(unittest.TestCase):
    def setUp(self) -> None:
        import _transport

        self.transport = _transport
        self.path = _transport._breaker_path()
        self.backup = self.path.read_bytes() if self.path.exists() else None
        _transport._breaker_write({"consecutive": 0, "open_until": 0.0, "reason": ""})

    def tearDown(self) -> None:
        if self.backup is None:
            self.path.unlink(missing_ok=True)
        else:
            self.path.write_bytes(self.backup)

    def test_a_success_clears_the_count(self) -> None:
        for _ in range(3):
            self.transport.breaker_record("RuntimeError: rate limited")
        self.assertEqual(self.transport._breaker_state()["consecutive"], 3)
        self.transport.breaker_record(None)
        self.assertEqual(self.transport._breaker_state()["consecutive"], 0)

    def test_it_opens_only_after_repeated_failure(self) -> None:
        """One bad call is noise; a run of them is an outage."""
        for _ in range(self.transport.BREAKER_THRESHOLD - 1):
            self.transport.breaker_record("RuntimeError: out of credits")
        self.transport.breaker_check()  # still closed

        self.transport.breaker_record("RuntimeError: out of credits")
        with self.assertRaises(RuntimeError) as caught:
            self.transport.breaker_check()
        self.assertIn("out of credits", str(caught.exception))

    def test_the_cooldown_widens_as_failures_persist(self) -> None:
        """A rate limit clears by itself; an expired credential does not."""
        for _ in range(self.transport.BREAKER_THRESHOLD):
            self.transport.breaker_record("boom")
        first = self.transport._breaker_state()["open_until"]
        for _ in range(self.transport.BREAKER_THRESHOLD):
            self.transport.breaker_record("boom")
        second = self.transport._breaker_state()["open_until"]
        self.assertGreater(second, first)

    def test_a_closed_breaker_never_blocks(self) -> None:
        self.transport.breaker_record(None)
        self.transport.breaker_check()

    def test_a_corrupt_breaker_file_does_not_stop_work(self) -> None:
        """Instrumentation must never be able to halt the pipeline."""
        self.path.write_text("not json", encoding="utf-8")
        self.assertEqual(self.transport._breaker_state()["consecutive"], 0)
        self.transport.breaker_check()


class SupervisorGuardTests(unittest.TestCase):
    """The supervisor must measure progress, not restarts."""

    def setUp(self) -> None:
        self.script = (ROOT / "scripts" / "run-gemini-batch.sh").read_text(encoding="utf-8")

    def test_it_halts_after_repeated_barren_passes(self) -> None:
        self.assertIn("MAX_BARREN_PASSES", self.script)
        self.assertIn("pass completed no work", self.script)
        self.assertIn("HALTED", self.script)

    def test_it_restarts_the_model_proxy_when_down(self) -> None:
        self.assertIn("ensure_proxy", self.script)
        self.assertIn("start-litellm.sh", self.script)

    def test_it_compares_outstanding_before_and_after(self) -> None:
        """Progress is a delta, not a process being alive."""
        self.assertIn('after="$(stat_of outstanding)"', self.script)
        self.assertIn('[ "${after:-0}" -ge "${left:-0}" ]', self.script)


if __name__ == "__main__":
    unittest.main()
