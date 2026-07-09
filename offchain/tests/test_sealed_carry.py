"""Sealed large-order carry-forward — a big sealed order accumulates fills across auctions.

A 6 s batch rarely holds enough crossing supply to fill a large order at once. Public/market
remainders already rest in the on-chain book; sealed remainders are IOC on-chain (the service
keeps them off the public book). So the BUILDER re-seals a partially-filled sealed order's
remainder into a fresh hidden commitment and carries it forward — the order keeps working over
successive rounds instead of losing the remainder to cancellation. This drives `api_round` with
monkeypatched chain I/O (no node).
"""
import os
import struct
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
        server._round_gate.clear()      # gate/in-flight state must not leak between tests
        server._inflight.clear()
        server.JAMKB_BACKPRESSURE = False
        server.REQUIRE_ORDER_SIG = False
        server.ENC_MODE = False
        server.EXECS_FILE = "/tmp/jamswap_test_carry_execs.json"
        server.footprint_octets = lambda: 0
        server.mstate = lambda p, m: 0
        self.sent = []
        server.submit = (lambda payload, check=None, detail="":
                         self.sent.append(payload))
        # on-chain resting book: a single sell of 10 @ 1.0 that a buy will cross.
        # The mixed-chain commit gate only reveals a sealed order whose commit is
        # already ON-CHAIN, so the storage stub also serves the commit set for
        # every order this test placed (self.commits).
        self.book = server.order_bytes(20, 1, SELL, 1 * S, 10 * S)
        self.commits = bytearray()
        server.storage = (lambda k:
                          self.book if k.startswith(b"book")
                          else bytes(self.commits) if k.startswith(b"commits")
                          else b"")

    def _place_sealed_buy(self, qty, price, ttl=3600):
        # build + seal a pending buy the way api_order would (bypassing its network guards).
        oid = server.next_oid[0]; server.next_oid[0] += 1
        o = {"account": 7, "oid": oid, "side": BUY, "price": price * S, "qty": qty * S,
             "type": "limit", "sealed": True, "address": ""}
        o["commit"] = server._seal_material(1, o)   # placement posts an owner-signed commit;
        # here the chain is mocked, so attaching the reveal material is all the round needs.
        self.commits += o["commit"] + struct.pack("<I", o["account"])   # commit "on-chain"
        server.order_expiry[(1, 7, oid)] = time.time() + ttl
        server.pending.setdefault(1, []).append(o)
        return oid

    def _settle(self, vol):
        # simulate the round accumulating on-chain: cv reaches the clearing volume,
        # then the resolver sweep fires the predicate and finalizes (receipts+carry)
        server.mstate = lambda p, m, v=vol * S: (v if p == b"cv" else 0)
        server._resolve_rounds_once()

    def test_receipts_are_settlement_contingent(self):
        # submit alone produces NO receipt and NO carry — the round is in flight.
        oid = self._place_sealed_buy(250, 1)
        r = server.api_round({"market": 1, "base": 1, "quote": 0})
        self.assertTrue(r.get("queued"), "filling round goes in flight, not receipted")
        self.assertEqual(server.api_executions({"account": 7})["executions"], [],
                         "no receipt before the chain confirms the round")
        self.assertFalse([o for o in server.pending.get(1, []) if o["oid"] == oid],
                         "order is in the in-flight round, not the mempool")

    def test_big_sealed_buy_carries_unfilled_remainder(self):
        oid = self._place_sealed_buy(250, 1)                 # buy 250 @1.0 vs one sell 10 @1.0
        server.api_round({"market": 1, "base": 1, "quote": 0})
        self._settle(vol=10)
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
        self._settle(vol=10)
        self.assertFalse([o for o in server.pending.get(1, []) if o["oid"] == oid],
                         "an expired order's remainder is dropped, not carried")
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "partial-cancelled")

    def test_fully_filled_sealed_order_is_not_carried(self):
        oid = self._place_sealed_buy(10, 1)                  # exactly matches the 10-lot sell
        server.api_round({"market": 1, "base": 1, "quote": 0})
        self._settle(vol=10)
        self.assertFalse([o for o in server.pending.get(1, []) if o["oid"] == oid])
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "filled")

    def test_unsettled_round_requeues_without_receipts(self):
        # the round never accumulates: past ROUND_GATE_SECS the resolver re-queues
        # its orders (nothing lost) and hands out NO fill receipts (no phantoms).
        oid = self._place_sealed_buy(250, 1)
        server.api_round({"market": 1, "base": 1, "quote": 0})
        self.assertIn(1, server._inflight)
        server._resolve_rounds_once(now=time.time() + server.ROUND_GATE_SECS + 1)
        self.assertNotIn(1, server._inflight)
        back = [o for o in server.pending.get(1, []) if o["oid"] == oid]
        self.assertEqual(len(back), 1, "order re-queued for the next auction")
        self.assertEqual(back[0]["qty"], 250 * S, "full quantity back — nothing filled")
        self.assertEqual(server.api_executions({"account": 7})["executions"], [],
                         "no receipts for a round that never settled")


if __name__ == "__main__":
    unittest.main(verbosity=2)
