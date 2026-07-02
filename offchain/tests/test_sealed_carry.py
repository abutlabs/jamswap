"""Sealed large-order carry-forward — a big sealed order accumulates fills across auctions.

A 6 s batch rarely holds enough crossing supply to fill a large order at once. Public/market
remainders already rest in the on-chain book; sealed remainders are IOC on-chain (the service
keeps them off the public book). So the BUILDER re-seals a partially-filled sealed order's
remainder into a fresh hidden commitment and carries it forward — the order keeps working over
successive rounds instead of losing the remainder to cancellation. This drives `api_round` with
monkeypatched chain I/O (no node).
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402
from clearing import BUY, SELL  # noqa: E402

S = server.SCALE


class SealedCarry(unittest.TestCase):
    def setUp(self):
        server.pending.clear(); server.order_expiry.clear(); server.executions.clear()
        server.JAMKB_BACKPRESSURE = False
        server.REQUIRE_ORDER_SIG = False
        server.ENC_MODE = False
        server.EXECS_FILE = "/tmp/jamswap_test_carry_execs.json"
        server.footprint_octets = lambda: 0
        server.mstate = lambda p, m: 0
        self.sent = []
        server.submit = lambda payload: self.sent.append(payload)
        # on-chain resting book: a single sell of 10 @ 1.0 that a buy will cross.
        self.book = server.order_bytes(20, 1, SELL, 1 * S, 10 * S)
        server.storage = lambda k: self.book if k.startswith(b"book") else b""

    def _place_sealed_buy(self, qty, price, ttl=3600):
        # build + seal a pending buy the way api_order would (bypassing its network guards).
        oid = server.next_oid[0]; server.next_oid[0] += 1
        o = {"account": 7, "oid": oid, "side": BUY, "price": price * S, "qty": qty * S,
             "type": "limit", "sealed": True, "address": ""}
        server._post_seal(1, o)
        server.order_expiry[(1, 7, oid)] = time.time() + ttl
        server.pending.setdefault(1, []).append(o)
        return oid

    def test_big_sealed_buy_carries_unfilled_remainder(self):
        oid = self._place_sealed_buy(250, 1)                 # buy 250 @1.0 vs one sell 10 @1.0
        server.api_round({"market": 1, "base": 1, "quote": 0})
        # 240 of the 250 carries forward as a fresh sealed pending order under the SAME oid.
        carried = [o for o in server.pending.get(1, []) if o["oid"] == oid]
        self.assertEqual(len(carried), 1, "remainder carried forward, not cancelled")
        self.assertEqual(carried[0]["qty"], 240 * S)
        self.assertTrue(carried[0]["sealed"], "carried remainder stays hidden")
        self.assertIn("reveal", carried[0], "re-sealed with a fresh commitment")
        # the receipt reads partial-carried (still working), not cancelled.
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "partial-carried")
        self.assertEqual(rec["filled"], 10)
        self.assertEqual(rec["remainder"], 240)

    def test_expired_remainder_is_not_carried(self):
        oid = self._place_sealed_buy(250, 1, ttl=-1)         # already past its good-till-time
        server.api_round({"market": 1, "base": 1, "quote": 0})
        self.assertFalse([o for o in server.pending.get(1, []) if o["oid"] == oid],
                         "an expired order's remainder is dropped, not carried")
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "partial-cancelled")

    def test_fully_filled_sealed_order_is_not_carried(self):
        oid = self._place_sealed_buy(10, 1)                  # exactly matches the 10-lot sell
        server.api_round({"market": 1, "base": 1, "quote": 0})
        self.assertFalse([o for o in server.pending.get(1, []) if o["oid"] == oid])
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "filled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
