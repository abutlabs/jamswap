"""What goes into a round's batch — the round builder's batch cap (jamswap#6).

Each auction used to cap the queue first (`_cap_batch`: the first MAX_ROUND_ORDERS) and
plan only the capped part, then rewrite the queue as `carry + deferred + overflow`. Sealed
orders that must wait — hidden (they cross nothing) or deferred (their commit hasn't
landed) — so sat at the head of the queue, took the cap every auction and kept every
public order behind them out of every round for their whole ~32 min life (the review's
probe: cap 4, four deferred sealed orders, a crossing public pair → no round in five
auctions). Now only orders that can trade this round compete for the cap (`round.plan_batch`).

Drives `server.py` against the fake chain of test_round_poison.py. Run with:
    python3 -m unittest discover -s offchain/tests
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server                                        # noqa: E402
from test_round_poison import _Base, order, M, S, u32   # noqa: E402  (the fake-chain harness)


class _Batch(_Base):
    def sealed(self, acct, oid, side, price, qty=1, committed=True):
        # a sealed order as api_order queues it; `committed`: its owner-signed commit is in
        # the on-chain commit set (else the readiness gate defers it)
        o = {"account": acct, "oid": oid, "side": server.BUY if side == "buy" else server.SELL,
             "price": int(price * S), "qty": int(qty * S), "type": "limit", "sealed": True,
             "address": ""}
        o["commit"] = server._seal_material(M, o)
        if committed:
            key = b"commits" + u32(M)
            self.chain.kv[key] = self.chain.kv.get(key, b"") + server._consumed_entry(o)
        server.order_expiry[(M, acct, oid)] = time.time() + 1900
        self.queue(o)
        return o

    def batch(self):
        # (public oids, revealed sealed oids) of the round in flight
        self.assertIn(M, server._inflight, "no round was submitted")
        fr = server._inflight[M]
        return sorted(o["oid"] for o in fr["public"]), sorted(o["oid"] for o in fr["sealed"])


class WaitingSealedOrdersTakeNoPlace(_Batch):
    def setUp(self):
        super().setUp()
        server.MAX_ROUND_ORDERS = 4

    def test_deferred_sealed_orders_do_not_starve_public_orders(self):
        # the review's probe: four sealed sells whose commits never land, then a crossing
        # public pair. They would cross the buy — the readiness gate still holds them.
        for i in range(4):
            self.sealed(9, 100 + i, "sell", 0.5, committed=False)
        self.crossing_pair()
        self.build()
        self.assertEqual(len(self.sent), 1, "the public pair goes in the next auction")
        self.assertEqual(self.batch(), ([1, 2], []))
        self.assertEqual(self.pending_oids(), [100, 101, 102, 103], "still waiting for their commits")

    def test_hidden_sealed_orders_do_not_starve_public_orders(self):
        # committed sealed sells no buy reaches, queued around the pair (cap 2 < 4 of them)
        server.MAX_ROUND_ORDERS = 2
        self.sealed(9, 100, "sell", 5)
        self.sealed(9, 101, "sell", 5)
        self.crossing_pair()
        self.sealed(9, 102, "sell", 5)
        self.sealed(9, 103, "sell", 5)
        self.build()
        self.assertEqual(self.batch(), ([1, 2], []))
        self.assertEqual(self.pending_oids(), [100, 101, 102, 103], "hidden, in queue order")

    def test_a_sealed_order_is_not_revealed_when_its_counterparty_missed_the_cap(self):
        # four committed sealed sells cross the public buy — queued fifth, over the cap. They
        # used to fill the batch and cross nothing in it (no round); revealing them there
        # would leak their terms for nothing. They wait, hidden; the batch is refilled.
        for i in range(4):
            self.sealed(9, 100 + i, "sell", 0.5)
        self.crossing_pair()
        self.build()
        self.assertEqual(self.batch(), ([1, 2], []))
        self.assertEqual(self.pending_oids(), [100, 101, 102, 103])

    def test_a_long_run_of_such_orders_gives_way_and_crosses_the_book_next_round(self):
        # twenty of them: more than the planner's bounded refills reach, so they give way to
        # the public orders; the buy's unfilled rest stays on the book, and they cross it in
        # the next round — the cap's four at a time
        for i in range(20):
            self.sealed(9, 100 + i, "sell", 0.5)
        self.queue(order(7, 1, "buy", 1, 20, seq=15), order(8, 2, "sell", 1, 10, seq=3))
        self.build()
        self.assertEqual(self.batch(), ([1, 2], []))
        self.chain.land(server._inflight[M])
        self.chain.set_book(server.order_bytes(7, 1, server.BUY, S, 10 * S))   # the buy's rest
        self.sweeps(0, 2)
        self.assertNotIn(M, server._inflight)
        self.build()
        self.assertEqual(self.batch(), ([], [100, 101, 102, 103]))

    def test_sealed_orders_that_can_trade_keep_their_place_in_the_queue(self):
        # a crossing sealed order still competes for the cap in arrival order
        self.sealed(9, 100, "sell", 1)
        self.queue(order(7, 1, "buy", 1, 10, seq=15))
        self.queue(*(order(10 + i, 10 + i, "sell", 3, 1, seq=1) for i in range(4)))
        self.build()
        self.assertEqual(self.batch(), ([1, 10, 11], [100]))
        self.assertEqual(self.pending_oids(), [12, 13])


if __name__ == "__main__":
    unittest.main(verbosity=2)
