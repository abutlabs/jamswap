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
        server._carry_retry.clear()
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
        # simulate the round accumulating on-chain AND SURVIVING the durability
        # hold: cv reaches the clearing volume, one sweep starts the hold window,
        # a second sweep past SETTLE_HOLD_SECS confirms (receipts + carry)
        server.mstate = lambda p, m, v=vol * S: (v if p == b"cv" else 0)
        t = time.time()
        server._resolve_rounds_once(now=t)
        server._resolve_rounds_once(now=t + server.SETTLE_HOLD_SECS + 1)

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

    def test_reorg_revert_holds_and_resettles(self):
        # the cv predicate fires, then a RE-ORG erases it before the hold window
        # passes: no receipts may be issued for the vanished settlement, and when
        # the chain re-applies it, the full hold restarts before confirming.
        oid = self._place_sealed_buy(250, 1)
        server.api_round({"market": 1, "base": 1, "quote": 0})
        t = time.time()
        server.mstate = lambda p, m: (10 * S if p == b"cv" else 0)   # settled...
        server._resolve_rounds_once(now=t)
        server.mstate = lambda p, m: 0                               # ...re-org erased it
        server._resolve_rounds_once(now=t + 10)
        self.assertEqual(server.api_executions({"account": 7})["executions"], [],
                         "no receipts for a settlement a re-org erased")
        self.assertIn(1, server._inflight, "round stays in flight awaiting re-settle")
        server.mstate = lambda p, m: (10 * S if p == b"cv" else 0)   # re-applied
        server._resolve_rounds_once(now=t + 20)                      # hold restarts here
        server._resolve_rounds_once(now=t + 20 + server.SETTLE_HOLD_SECS + 1)
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "partial-carried")
        self.assertEqual(rec["filled"], 10)
        self.assertFalse(1 in server._inflight)
        del oid

    def test_carry_retry_when_chain_busy_instead_of_dropping(self):
        # R4: if the carry-commit can't be posted (all guarantor queues full), the
        # remainder is NOT dropped — it's queued for retry and re-seals once the chain
        # drains. The receipt reads partial-carried with a reason, never a silent cancel.
        oid = self._place_sealed_buy(250, 1)
        server.api_round({"market": 1, "base": 1, "quote": 0})
        busy = {"on": True}
        def submit_busy(payload, check=None, detail=""):
            if payload[0] == server.TAG_CARRY_COMMIT and busy["on"]:
                raise server.ChainBusy("CE-133 queues full")
            self.sent.append(payload)
        server.submit = submit_busy
        self._settle(vol=10)
        self.assertIn(1, server._carry_retry, "remainder queued for retry, not dropped")
        self.assertEqual(len(server._carry_retry[1]), 1)
        self.assertFalse([o for o in server.pending.get(1, []) if o["oid"] == oid],
                         "not in the mempool yet — waiting on the re-seal")
        rec = server.api_executions({"account": 7})["executions"][0]
        self.assertEqual(rec["disposition"], "partial-carried")
        self.assertEqual(rec["reason"], "re-seal queued (chain busy)")
        # chain drains -> next sweep re-seals and the remainder enters the mempool.
        busy["on"] = False
        server._resolve_rounds_once(now=time.time())
        self.assertNotIn(1, server._carry_retry, "retry queue drained")
        carried = [o for o in server.pending.get(1, []) if o["oid"] == oid]
        self.assertEqual(len(carried), 1, "remainder re-seals on retry — never lost")
        self.assertEqual(carried[0]["qty"], 240 * S)

    def test_carry_retry_expiring_before_reseal_is_cancelled_with_reason(self):
        # The one place a carried remainder ends without filling: its GTT elapses while
        # the chain is still too busy to accept the re-seal. It must be SURFACED with a
        # reason (terminal cancelled), never a silent vanish.
        oid = self._place_sealed_buy(250, 1, ttl=3600)
        server.api_round({"market": 1, "base": 1, "quote": 0})
        server.submit = (lambda payload, check=None, detail="":
                         (_ for _ in ()).throw(server.ChainBusy("busy"))
                         if payload[0] == server.TAG_CARRY_COMMIT
                         else self.sent.append(payload))
        self._settle(vol=10)
        self.assertIn(1, server._carry_retry)
        server.order_expiry[(1, 7, oid)] = time.time() - 1     # GTT now in the past
        server._resolve_rounds_once(now=time.time())
        self.assertNotIn(1, server._carry_retry, "expired remainder removed from retry")
        cancels = [r for r in server.api_executions({"account": 7})["executions"]
                   if r["disposition"] == "cancelled"]
        self.assertTrue(cancels, "expired-before-reseal surfaces a terminal receipt")
        self.assertEqual(cancels[0]["reason"], "expired-before-reseal")

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


class SealedReadyFinality(unittest.TestCase):
    """Phase 2: reveal only β-FINALIZED commits. A finalized commit can't re-org out, so
    a revealed round can't be rolled back for a vanished commit. Strict improvement over
    Phase-1 best-chain membership; falls back to membership on a non-finalizing chain."""

    def setUp(self):
        server._commit_seen.clear()
        server.ENC_MODE = False

    def _order(self, acct=7, oid=1):
        o = {"account": acct, "oid": oid, "side": BUY, "price": 1 * S, "qty": 10 * S,
             "type": "limit", "sealed": True}
        o["commit"] = server._seal_material(1, o)
        return o

    def _entry(self, o):
        return server.commitment(o["reveal"]) + struct.pack("<I", o["account"])

    def test_no_finality_falls_back_to_membership(self):
        o, off = self._order(), self._order(acct=8, oid=2)
        ready = server._sealed_ready_predicate(1, {self._entry(o)}, {"available": False})
        self.assertTrue(ready(o), "on-chain commit ready when finality unavailable")
        self.assertFalse(ready(off), "off-chain commit never ready")

    def test_onchain_but_not_final_defers(self):
        o = self._order()
        ready = server._sealed_ready_predicate(
            1, {self._entry(o)}, {"available": True, "block_height": 100, "finalized_height": 99})
        self.assertFalse(ready(o), "seen at head 100 but finalized only 99 -> defer")

    def test_finalized_is_ready(self):
        o = self._order(); e = {self._entry(o)}
        server._sealed_ready_predicate(
            1, e, {"available": True, "block_height": 100, "finalized_height": 98})   # pins seen=100
        ready = server._sealed_ready_predicate(
            1, e, {"available": True, "block_height": 101, "finalized_height": 100})  # β caught up
        self.assertTrue(ready(o), "finalized_height reached the pinned height -> ready")

    def test_commit_leaving_set_is_forgotten(self):
        o = self._order()
        server._sealed_ready_predicate(
            1, {self._entry(o)}, {"available": True, "block_height": 100, "finalized_height": 100})
        self.assertEqual(len(server._commit_seen[1]), 1)
        server._sealed_ready_predicate(
            1, set(), {"available": True, "block_height": 101, "finalized_height": 101})
        self.assertEqual(len(server._commit_seen[1]), 0, "consumed/expired commit forgotten")


if __name__ == "__main__":
    unittest.main(verbosity=2)
