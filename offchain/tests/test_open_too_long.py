"""order_telemetry.snapshot(): the live count of orders open too long matches what the
soak verdict calls stuck open (soak_verdict.score, open_grace), so a backlog shows on the
DEX dashboard while it builds, not only in the verdict at the end.

    python3 -m unittest discover -s offchain/tests -p 'test_open_too_long.py'
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import metrics                                       # noqa: E402
import order_telemetry as ot                         # noqa: E402
import soak_verdict as sv                            # noqa: E402

M = 7


class OpenTooLong(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(prefix="jamswap_test_open_", suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.remove, self.path)
        self.saved = (ot.ORDER_EVENTS_FILE, dict(ot._orders), dict(ot._slo))
        ot.ORDER_EVENTS_FILE = self.path
        ot._orders.clear()

    def tearDown(self):
        ot.ORDER_EVENTS_FILE = self.saved[0]
        ot._orders.clear()
        ot._orders.update(self.saved[1])
        ot._slo.clear()
        ot._slo.update(self.saved[2])

    def gauge(self, name, labels=None):
        return metrics._gauges.get((name, metrics._labels_key(labels)))

    def test_the_live_gauge_counts_what_the_verdict_calls_stuck(self):
        t0 = 1790000000.0
        clock = [t0]
        with mock.patch.object(ot.time, "time", lambda: clock[0]):
            ot.placed(M, 1, 1, 0, 100, 5, False, True)      # silent from here on: stuck
            ot.placed(M, 1, 2, 0, 100, 5, False, True)
            ot.placed(M, 1, 3, 1, 90, 5, False, False)
            ot.rested(M, 1, 3, 95, False, expires_at=t0 + 3600)   # on the book until expiry
            clock[0] = t0 + 500
            ot.rounded(M, 1, 2)                               # progressed: not stuck
            now = t0 + ot.OPEN_GRACE + 1
            snap = ot.snapshot(now=now)
        self.assertEqual(snap["stale"], 1)
        self.assertEqual(self.gauge("jamswap_order_open_stale", {"phase": "placed"}), 1)
        self.assertEqual(self.gauge("jamswap_order_open_stale", {"phase": "rounded"}), 0)
        self.assertEqual(self.gauge("jamswap_order_open_stale", {"phase": "rested"}), 0)
        self.assertAlmostEqual(self.gauge("jamswap_order_open_oldest_seconds"), ot.OPEN_GRACE + 1)
        # the verdict, reading the same event log at the same moment, calls the same order stuck
        report = sv.score(sv.load(self.path), now=now)
        self.assertEqual(report["stuck_open_count"], 1)
        self.assertEqual(str(report["stuck_open"][0][0][-1]), "1")

    def test_the_grace_is_the_verdicts(self):
        self.assertEqual(sv.score.__defaults__[1], ot.OPEN_GRACE)


if __name__ == "__main__":
    unittest.main()
