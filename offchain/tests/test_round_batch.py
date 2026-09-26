"""What goes into a round's batch — the round builder's cap (jamswap#6) and its reveal
and gas checks (jamswap#8).

Each auction used to cap the queue first (`_cap_batch`: the first MAX_ROUND_ORDERS) and
plan only the capped part, then rewrite the queue as `carry + deferred + overflow`. Sealed
orders that must wait — hidden (they cross nothing) or deferred (their commit hasn't
landed) — so sat at the head of the queue, took the cap every auction and kept every
public order behind them out of every round for their whole ~32 min life (the review's
probe: cap 4, four deferred sealed orders, a crossing public pair → no round in five
auctions). Now only orders that can trade this round compete for the cap (`round.plan_batch`).

jamswap#8: the reveals were decided before the public orders were priced, sanitized and
claimed, so a sealed order could be revealed with no counterparty left; they are now
re-checked against the orders the round submits. And the count cap didn't bound an
encrypt-until-batch round (n x 18.7M gas per sealed order: ~26 fill G_R = 1e9 at n = 2),
which then never landed and was rebuilt at the same size forever; rounds are now bounded
by refine gas too, and a timed-out one is rebuilt smaller.

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
            key = (b"encset" if server.ENC_MODE else b"commits") + u32(M)
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


class RevealsAreRecheckedBeforeSubmit(_Batch):
    """A sealed order is revealed only if a counterparty is among the orders the round
    actually submits — after market orders are priced, stale / repeated seqs dropped and a
    late landing's orders claimed. Revealed alone, it trades nothing and its terms leak."""

    def test_not_revealed_when_its_counterparty_is_superseded(self):
        self.chain.set_floor(b"sq", 7, 20)                  # acct 7 already settled seq 20
        self.sealed(9, 100, "sell", 1)
        self.queue(order(7, 1, "buy", 1, 10, seq=15))
        self.build()
        self.assertEqual(self.sent, [], "no round: the sealed sell would trade nothing")
        self.assertEqual(self.pending_oids(), [100], "back in the queue, still hidden")
        self.assertEqual(self.dispositions(7), ["rejected"])

    def test_not_revealed_when_its_counterparty_is_a_market_order_with_no_last_price(self):
        self.sealed(9, 100, "sell", 1)
        self.queue(order(7, 1, "buy", 1.1, 10, seq=15, otype="market"))
        self.build()
        self.assertEqual(self.sent, [])
        self.assertEqual(self.pending_oids(), [100])
        self.assertIn("no last price", self.receipts(7)[0]["reason"])

    def test_not_revealed_when_its_counterparty_is_repriced_away(self):
        # the market buy was placed at 1.10 (last price 1); the last price has since fallen
        # to 0.50, so it is priced at 0.55 — below the sealed sell's 1
        self.lp = S // 2
        self.sealed(9, 100, "sell", 1)
        self.queue(order(7, 1, "buy", 1.1, 10, seq=15, otype="market"))
        self.build()
        self.assertEqual(self.batch(), ([1], []), "the buy goes alone; the sell stays hidden")
        self.assertEqual(server._inflight[M]["public"][0]["price"], 5500)
        self.assertEqual(self.pending_oids(), [100])

    def test_not_revealed_when_a_late_landing_claims_its_counterparty(self):
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.sealed(9, 100, "sell", 1)                       # crosses the pair's buy
        real_get, fired = self.chain.get, []

        def landing(key):                                    # R1 lands during the floor reads
            if key == b"sq" + u32(7) and not fired:
                fired.append(1)
                self.chain.land(r1)
            return real_get(key)
        server.storage = landing
        self.build()
        server.storage = real_get
        self.assertTrue(fired)
        self.assertEqual(len(self.sent), 1, "only R1 was ever submitted")
        self.assertEqual(self.pending_oids(), [100])

    def test_still_revealed_while_a_counterparty_remains(self):
        self.chain.set_floor(b"sq", 7, 20)
        self.sealed(9, 100, "sell", 1)
        self.queue(order(7, 1, "buy", 1, 10, seq=15), order(8, 2, "buy", 1, 10, seq=3))
        self.build()
        self.assertEqual(self.batch(), ([2], [100]))


class EncryptUntilBatchRoundsAreBoundedByGas(_Batch):
    """n x 18.7M refine gas per encrypt-until-batch order: at n = 2, 21 of them fit the
    round budget (80% of G_R = 1e9: 21 x 37.4M = 785M; 22 would be 823M)."""

    def setUp(self):
        super().setUp()
        server._enc_cap.clear()
        self.addCleanup(server._enc_cap.clear)
        server.ENC_MODE = True

        def committee(cmd, *args):
            if cmd == "encrypt":                             # (market, order hex, seed)
                return {"ciphertext": (b"\x02" * 32 + bytes.fromhex(args[1])).hex()}
            if cmd == "round":                               # (m, base, quote, section, cts)
                return {"round": (bytes([server.TAG_ENC_ROUND]) + args[4].encode()
                                  + bytes.fromhex(args[3])).hex()}
            raise AssertionError(cmd)
        server.committee_run = committee
        self.committee(2)
        self.chain.set_book(server.order_bytes(20, 1, server.SELL, S, 1000 * S))

    def committee(self, n):
        self.chain.kv[b"committee"] = bytes([n]) + bytes(32 * n)

    def buys(self, k):
        for i in range(k):
            self.sealed(7, 100 + i, "buy", 1)

    def sealed_in_flight(self):
        self.assertIn(M, server._inflight, "no round was submitted")
        return len(server._inflight[M]["sealed"])

    def test_a_round_carries_no_more_than_the_gas_budget_allows(self):
        self.buys(30)
        self.build()
        self.assertEqual(self.sealed_in_flight(), 21)
        self.assertEqual(len(self.pending_oids()), 9, "the rest wait for the next round")

    def test_the_committee_size_is_read_from_the_chain(self):
        self.committee(4)                                    # 74.8M each: 10 fit
        self.buys(12)
        self.build()
        self.assertEqual(self.sealed_in_flight(), 10)

    def test_a_timed_out_round_is_rebuilt_smaller_and_grows_back_once_one_lands(self):
        self.buys(8)
        sizes = []
        for _ in range(3):
            self.build()
            sizes.append(self.sealed_in_flight())
            if len(sizes) < 3:
                self.time_out()
        self.assertEqual(sizes, [8, 4, 2], "halved per timeout, not rebuilt at the same size")
        self.chain.land(server._inflight[M])
        self.sweeps(0, 2)
        self.assertNotIn(M, server._inflight)
        self.build()
        self.assertEqual(self.sealed_in_flight(), 4, "doubled once a round of 2 landed")

    def test_the_cap_bounds_gas_and_passes_over_an_order_that_never_fits(self):
        orders = [{"account": 1, "oid": i, "sealed": True, "g": g}
                  for i, g in enumerate((50, 200, 40, 30))]
        gas = lambda o: o["g"]
        batch, rest = server._cap_batch(orders, 10, gas=gas, budget=100)
        # oid 1 alone needs more than the budget: passed over; 0 + 2 = 90; 3 would be 120
        self.assertEqual(([o["oid"] for o in batch], [o["oid"] for o in rest]), ([0, 2], [1, 3]))
        batch, _ = server._cap_batch(orders, 10, gas=gas, budget=100, sealed_cap=1)
        self.assertEqual([o["oid"] for o in batch], [0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
