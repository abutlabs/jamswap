"""Execution-report tests — the per-order fill receipts the UI shows.

`record_executions` recomputes a round's clearing (builder-side, mirroring refine) and writes
one receipt per trader order. Here we call it directly with hand-built order sets (no node) and
assert the receipts, then check `api_executions` returns them per account. The clearing parity
itself is covered by `test_clearing.py`.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402
from clearing import BUY, SELL  # noqa: E402


def o(account, oid, side, price, qty):
    return {"account": account, "oid": oid, "side": side, "price": price, "qty": qty}


class Executions(unittest.TestCase):
    def setUp(self):
        server.EXECS_FILE = "/tmp/jamswap_test_execs.json"   # throwaway
        server.executions.clear()

    def test_big_sealed_buy_partial_fill_then_cancel(self):
        # the user's scenario, sealed: BUY 500@1.25 (as a revealed sealed order) vs resting
        # asks 100 each @1.10/1.20/1.30. Clears 200 @ 1.20 uniform; 300 of the buy is dropped
        # (sealed remainder is immediate-or-cancel).
        S = server.SCALE
        resting = [o(10, 1, SELL, int(1.10 * S), 100 * S),
                   o(11, 2, SELL, int(1.20 * S), 100 * S),
                   o(12, 3, SELL, int(1.30 * S), 100 * S)]
        buyer = o(99, 4, BUY, int(1.25 * S), 500 * S)
        server.record_executions(1, resting, reveal=[buyer], public=[])
        recs = server.api_executions({"account": 99})["executions"]
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["side"], BUY)
        self.assertEqual(r["qty"], 500)
        self.assertEqual(r["filled"], 200)
        self.assertEqual(r["price"], 1.2, "uniform clearing price, not per-level")
        self.assertEqual(r["remainder"], 300)
        self.assertEqual(r["disposition"], "partial-cancelled", "sealed remainder is IOC-dropped")

    def test_resting_makers_get_receipts_too(self):
        S = server.SCALE
        resting = [o(10, 1, SELL, int(1.10 * S), 100 * S),
                   o(11, 2, SELL, int(1.20 * S), 100 * S)]
        buyer = o(99, 4, BUY, int(1.25 * S), 500 * S)
        server.record_executions(1, resting, reveal=[buyer], public=[])
        # both asks fully filled at the 1.20 uniform price → each maker sees a 'filled' receipt
        for maker in (10, 11):
            recs = server.api_executions({"account": maker})["executions"]
            self.assertEqual(len(recs), 1)
            self.assertEqual(recs[0]["disposition"], "filled")
            self.assertEqual(recs[0]["price"], 1.2)

    def test_public_order_that_fully_rests_gets_no_receipt(self):
        # a public buy under the best ask doesn't cross → it just rests (shown in YOUR ORDERS),
        # so no execution receipt is emitted.
        S = server.SCALE
        resting = [o(10, 1, SELL, int(1.30 * S), 100 * S)]
        buyer = o(99, 5, BUY, int(1.10 * S), 100 * S)
        server.record_executions(1, resting, reveal=[], public=[buyer])
        self.assertEqual(server.api_executions({"account": 99})["executions"], [])

    def test_persist_and_reload(self):
        S = server.SCALE
        server.record_executions(1, [o(10, 1, SELL, S, 5 * S)], reveal=[o(99, 6, BUY, S, 5 * S)], public=[])
        server.executions.clear()
        server.load_execs()
        self.assertEqual(len(server.api_executions({"account": 99})["executions"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
