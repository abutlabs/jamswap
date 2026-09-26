"""Round poisoning + late settlement — the 2026-09-24 soak failure and its fixes.

The soak (lasair6, clearing SLO 0.49) failed because a trader's sealed COMMIT raised the
same per-account seq floor as signed public orders. The commit is a small standalone
work-item, so it usually landed before that trader's OLDER public orders, which wait for a
round; the round carrying them then failed its accumulate check WHOLE, the builder waited out
the full gate, re-queued the round to the TAIL (newer orders of the same account overtook and
stranded the old ones), and finally recorded the stale orders "rejected" with no receipt.

Fixes pinned here, driving `server.py` against a fake chain (monkeypatched storage/submit,
same style as test_sealed_carry.py):

  * the commit floor is separate (b"sc"): a landed commit no longer poisons the round;
  * a round that can no longer settle (book moved / consumed commit gone / an account's
    floor passed its oldest order) is released within seconds, its orders to the FRONT;
  * a released round that settles late (its round id is marked landed) is finalized —
    filled, not rejected — after a second sighting, and its orders are not re-submitted;
    a re-org that erases the landing (on either side of a two-fork flip) undoes the claim;
  * one record per round id: an unchanged rebuild of a released round is that round;
  * a build and the resolver never interleave on one market (no double terminals);
  * the batch cap never lets an account's newer order ahead of its older one; repeated
    seqs and out-of-band market prices can't sink a round;
  * a truly superseded order ends with a truthful receipt, not a silent vanish;
  * signed cancels / mempool cancels end the order's lifecycle in the telemetry, and never
    contradict a fill;
  * the server's round id is the service's (shared fixture), over the exact payload bytes.

Run with:  python3 -m pytest -q offchain/tests
"""
import hashlib
import os
import struct
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server           # noqa: E402
import order_telemetry  # noqa: E402

M = 1
S = server.SCALE
PK = bytes([0x11]) * 32


def u32(x):
    return struct.pack("<I", x)


class FakeChain:
    """The service storage the builder reads, and the state changes a settling round makes."""

    def __init__(self):
        self.kv = {}

    def get(self, key):
        return self.kv.get(bytes(key), b"")

    def set_floor(self, prefix, acct, v):          # b"sq" (orders) / b"sc" (commits)
        self.kv[prefix + u32(acct)] = struct.pack("<Q", v)

    def set_book(self, raw):
        self.kv[b"book" + u32(M)] = raw

    def mark(self, fr, slot=1):
        # the landed marker alone (as seen at the head of a fork)
        self.kv[b"rl" + fr["rid"]] = u32(slot)

    def unmark(self, fr):
        self.kv.pop(b"rl" + fr["rid"], None)

    def land(self, fr, slot=1):
        # what the service's accumulate does when it accepts round `fr`: the orders' floors
        # rise to the round's highest seqs, its consumed commits leave the set, and its id
        # is marked landed at this slot
        for o in fr["public"]:
            key = b"sq" + u32(o["account"])
            cur = int.from_bytes(self.kv.get(key, b""), "little")
            self.kv[key] = struct.pack("<Q", max(cur, o["seq"]))
        ck = fr["set_key"] + u32(M)
        have = self.kv.get(ck, b"")
        for e in fr["consumed"]:
            i = have.find(e)
            if i >= 0:
                have = have[:i] + have[i + len(e):]
        self.kv[ck] = have
        self.mark(fr, slot)


def order(acct, oid, side, price, qty, seq, otype="limit"):
    # a signed public order as api_order queues it (signature framed, never verified here)
    return {"account": acct, "oid": oid, "side": server.BUY if side == "buy" else server.SELL,
            "price": int(price * S), "qty": int(qty * S), "sealed": False, "address": "",
            "type": otype, "signed_price": 0 if otype == "market" else int(price * S),
            "seq": seq, "pubkey": PK, "sig": bytes(64)}


class _Base(unittest.TestCase):
    PATCH = ("submit", "storage", "mstate", "expired_pairs", "nonce_of", "signer_key",
             "committee_run", "MAX_ROUND_ORDERS", "SETTLE_HOLD_SECS", "ROUND_GATE_SECS",
             "DEAD_CONFIRM_SECS", "ZOMBIE_WATCH_SECS", "EXECS_FILE", "REQUIRE_ORDER_SIG",
             "ENC_MODE", "JAMKB_BACKPRESSURE", "pubkey_of_handle", "MAX_OPEN_ORDERS")
    STATE = ("pending", "order_expiry", "executions", "_round_gate", "_inflight", "_zombies",
             "_carry_retry", "_commit_seen")

    def setUp(self):
        self._saved = {n: getattr(server, n) for n in self.PATCH}
        self._saved_events = order_telemetry.ORDER_EVENTS_FILE
        tmp = tempfile.gettempdir()
        order_telemetry.ORDER_EVENTS_FILE = os.path.join(tmp, "jamswap_test_poison_events.jsonl")
        server.EXECS_FILE = os.path.join(tmp, "jamswap_test_poison_execs.json")
        self.chain = FakeChain()
        self.sent = []
        server.storage = self.chain.get
        server.submit = lambda payload, check=None, detail="": self.sent.append(payload)
        self.lp = 0
        server.mstate = lambda prefix, m: self.lp if prefix == b"lp" else 0
        server.expired_pairs = lambda m, raw: []
        server.signer_key = lambda h: PK
        server.REQUIRE_ORDER_SIG = False
        server.ENC_MODE = False
        server.JAMKB_BACKPRESSURE = False
        server.SETTLE_HOLD_SECS = 0            # the lasair6 setting: finality replaces the hold
        server.ROUND_GATE_SECS = 180
        server.DEAD_CONFIRM_SECS = 4
        self._clear()

    def _clear(self):
        for st in self.STATE:
            getattr(server, st).clear()
        server._cancel_watch.clear()

    def tearDown(self):
        for n, v in self._saved.items():
            setattr(server, n, v)
        order_telemetry.ORDER_EVENTS_FILE = self._saved_events
        self._clear()

    # helpers ------------------------------------------------------------------
    def queue(self, *orders):
        for o in orders:
            server.pending.setdefault(M, []).append(o)
            order_telemetry.placed(M, o["account"], o["oid"], o["side"], o["price"], o["qty"],
                                   o["sealed"], True)

    def build(self):
        return server.api_round({"market": M, "base": 1, "quote": 0})

    def receipts(self, acct):
        return server.api_executions({"account": acct})["executions"]

    def dispositions(self, acct):
        return [r["disposition"] for r in self.receipts(acct)]

    def pending_oids(self):
        return [o["oid"] for o in server.pending.get(M, [])]

    def cool(self):
        # let a timeout's cooldown lapse so the next auction may build (the timeout sweeps
        # run at a future `now`, so the stamp is simply dropped)
        server._round_gate.pop(M, None)

    def time_out(self):
        # release the round in flight as "timeout" (the chain never included it)
        fr = server._inflight[M]
        server._resolve_rounds_once(now=time.time() + server.ROUND_GATE_SECS + 1)
        self.assertNotIn(M, server._inflight)
        self.cool()
        return fr

    def sweeps(self, *offsets):
        t = time.time()
        for dt in offsets:
            server._resolve_rounds_once(now=t + dt)

    def crossing_pair(self):
        self.queue(order(7, 1, "buy", 1, 10, seq=15), order(8, 2, "sell", 1, 10, seq=3))

    def sealed_buy(self, acct, oid, qty, price=1):
        o = {"account": acct, "oid": oid, "side": server.BUY, "price": int(price * S),
             "qty": int(qty * S), "type": "limit", "sealed": True, "address": ""}
        o["commit"] = server._seal_material(M, o)
        return o


class CommitFloorIsSeparate(_Base):
    def test_a_landed_commit_no_longer_poisons_the_round(self):
        # acct 7's public buy (seq 15) is in flight; its sealed order's commit (seq 20)
        # lands first. The service now raises only the COMMIT floor, so the round is
        # neither dead nor released — and settles, filling both sides.
        self.crossing_pair()
        self.build()
        fr = server._inflight[M]
        self.chain.set_floor(b"sc", 7, 20)                  # the commit landed (b"sc" only)
        t = time.time()
        server._resolve_rounds_once(now=t)
        server._resolve_rounds_once(now=t + server.DEAD_CONFIRM_SECS + 1)
        self.assertIs(server._inflight.get(M), fr, "the commit left the round alive")
        self.chain.land(fr)
        server._resolve_rounds_once(now=t + 10)
        server._resolve_rounds_once(now=t + 12)
        self.assertNotIn(M, server._inflight)
        self.assertEqual(self.dispositions(7), ["filled"])
        self.assertEqual(self.dispositions(8), ["filled"])

    def test_the_builder_judges_order_staleness_by_the_order_floor_only(self):
        self.chain.set_floor(b"sc", 7, 20)
        kept, dead, floors = server._seq_sanitize(M, [order(7, 1, "buy", 1, 10, seq=15)])
        self.assertEqual(([o["seq"] for o in kept], dead, floors), ([15], [], {7: 0}))


class DeadRoundReleasedEarly(_Base):
    def _in_flight(self):
        self.crossing_pair()
        self.build()
        return server._inflight[M]

    def _released_after_confirm(self):
        t = time.time()
        server._resolve_rounds_once(now=t)
        self.assertIn(M, server._inflight, "one dead sighting is not enough (re-org guard)")
        server._resolve_rounds_once(now=t + server.DEAD_CONFIRM_SECS)
        self.assertNotIn(M, server._inflight, "released within seconds, not after the gate")
        self.assertLess(server.DEAD_CONFIRM_SECS, server.ROUND_GATE_SECS)

    def test_seq_floor_passed_releases_the_round_to_the_front(self):
        self._in_flight()
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))       # arrives while the round flies
        self.chain.set_floor(b"sq", 7, 16)                  # a newer acct-7 order settled elsewhere
        self._released_after_confirm()
        self.assertEqual(self.pending_oids(), [1, 2, 3], "released orders go BEFORE newer ones")
        self.assertEqual(self.receipts(7), [], "no receipts for a round that never settled")
        self.assertNotIn(M, server._round_gate, "a dead round is rebuilt at once (no cooldown)")
        self.assertEqual(server._zombies[M][0]["why"], "seq-floor")

    def test_the_floor_is_judged_against_each_accounts_OLDEST_order(self):
        # acct 7 has seq 5 and seq 10 in the round; its floor rises to 7 (another market):
        # the seq-5 binding now fails accumulate, so the round is dead even though seq 10
        # still beats the floor
        self.queue(order(7, 1, "buy", 1, 5, seq=5), order(7, 2, "buy", 1, 5, seq=10),
                   order(8, 3, "sell", 1, 10, seq=3))
        self.build()
        self.assertEqual(server._inflight[M]["minseq"][7], 5)
        self.chain.set_floor(b"sq", 7, 7)
        self._released_after_confirm()
        self.assertEqual(server._zombies[M][0]["why"], "seq-floor")

    def test_book_moved_releases_the_round(self):
        self._in_flight()
        self.chain.set_book(server.order_bytes(5, 99, server.SELL, 2 * S, S))   # a cancel/round
        self._released_after_confirm()
        self.assertEqual(server._zombies[M][0]["why"], "book-moved")

    def test_consumed_commit_gone_releases_a_sealed_round(self):
        o = self.sealed_buy(7, 1, 10)
        entry = server.commitment(o["reveal"]) + u32(7)
        self.chain.kv[b"commits" + u32(M)] = entry           # the owner's commit is on-chain
        self.chain.set_book(server.order_bytes(8, 2, server.SELL, S, 10 * S))
        self.queue(o)
        self.build()
        self.assertEqual(server._inflight[M]["consumed"], [entry])
        self.chain.kv[b"commits" + u32(M)] = b""             # consumed/expired meanwhile
        self._released_after_confirm()
        self.assertEqual(server._zombies[M][0]["why"], "commit-gone")

    def test_a_dead_signal_that_clears_is_not_acted_on(self):
        self._in_flight()
        t = time.time()
        self.chain.set_floor(b"sq", 7, 16)
        server._resolve_rounds_once(now=t)
        self.chain.set_floor(b"sq", 7, 0)                    # a re-org restored the floor
        server._resolve_rounds_once(now=t + 2)
        server._resolve_rounds_once(now=t + server.DEAD_CONFIRM_SECS + 2)
        self.assertIn(M, server._inflight, "still landable: keep waiting")

    def test_a_live_round_still_times_out_after_the_gate(self):
        self._in_flight()
        server._resolve_rounds_once(now=time.time() + server.ROUND_GATE_SECS + 1)
        self.assertNotIn(M, server._inflight)
        self.assertEqual(server._zombies[M][0]["why"], "timeout")
        self.assertIn(M, server._round_gate, "timeout cools down (the chain never included it)")

    def test_zero_fill_round_is_tracked_and_judged_without_a_settle_predicate(self):
        # a lone non-crossing buy: nothing fills, the round only rests it. It used to be
        # fire-and-forget (lost if it never landed); now it is in flight like any round, and
        # dead-round detection works without any cv predicate (there is none to call).
        self.queue(order(7, 1, "buy", 0.5, 1, seq=1))
        self.build()
        fr = server._inflight[M]
        self.assertEqual(fr["clearing"]["volume"], 0)
        self.assertNotIn("check", fr)
        self.chain.set_book(server.order_bytes(5, 99, server.SELL, 2 * S, S))
        self._released_after_confirm()
        self.assertEqual(self.pending_oids(), [1], "nothing silently lost")


class LateLanding(_Base):
    def test_a_released_round_that_settles_late_is_finalized_not_resubmitted(self):
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.assertEqual(self.pending_oids(), [1, 2])
        self.chain.land(r1)                                  # ...and then lasair includes it
        self.build()                                         # the auction sees it first
        self.assertEqual(len(self.sent), 1, "its orders are NOT submitted again")
        self.assertEqual(self.pending_oids(), [])
        self.assertEqual(self.receipts(7), [], "claimed, not yet receipted (one sighting)")
        self.sweeps(2)                                       # the second sighting finalizes
        for acct in (7, 8):
            self.assertEqual(self.dispositions(acct), ["filled"], "filled, not rejected")
        self.assertNotIn(M, server._zombies, "finalized: no longer watched")
        self.assertFalse(order_telemetry.is_open(M, 7, 1))

    def test_late_landing_while_the_rebuilt_round_flies(self):
        # R1 times out, its orders are rebuilt into R2 with a fresh order; THEN R1 lands.
        # R1 is finalized (filled); R2 can't settle any more and is released, re-queueing
        # only the fresh order — never R1's already-filled ones.
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))
        self.build()
        r2 = server._inflight[M]
        self.assertEqual(sorted(o["oid"] for o in r2["public"]), [1, 2, 3])
        self.chain.land(r1)
        self.sweeps(0, server.DEAD_CONFIRM_SECS)
        self.assertNotIn(M, server._inflight, "R2 released: R1 raised acct 7's floor")
        self.assertEqual(self.pending_oids(), [3], "only the fresh order waits again")
        for acct in (7, 8):
            self.assertEqual(self.dispositions(acct), ["filled"])
        self.assertEqual(self.receipts(9), [])
        self.assertEqual(sorted(o["oid"] for o in r2["public"]), [1, 2, 3],
                         "R2's own record is never stripped (a re-org may hand them back)")

    def test_a_late_landing_erased_by_a_reorg_puts_the_orders_back(self):
        server.SETTLE_HOLD_SECS = 30
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.chain.mark(r1)
        self.sweeps(2)
        self.assertEqual(self.pending_oids(), [], "landed: claimed while its hold runs")
        self.assertEqual({o["oid"] for o in server.api_mine({"account": 7})["orders"]}, {1},
                         "still visible to its owner (settling)")
        self.chain.unmark(r1)                                # the re-org erased it
        self.sweeps(4)
        self.assertEqual(self.pending_oids(), [1, 2], "back in the mempool, at the front")
        self.assertEqual(self.receipts(7), [])

    def test_with_no_hold_a_late_landing_seen_once_is_not_receipted(self):
        # SETTLE_HOLD_SECS=0 used to finalize a zombie on the very sweep that first saw it
        # (irreversible: receipts, carries); a landing seen once on a fork that then lost
        # was a phantom fill. Now, like the round in flight, it needs a second sighting.
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.chain.mark(r1)
        self.sweeps(2)
        self.assertEqual(self.receipts(7), [])
        self.chain.unmark(r1)
        self.sweeps(4)
        self.assertEqual(self.pending_oids(), [1, 2])
        self.assertEqual(self.receipts(7), [])
        self.assertTrue(order_telemetry.is_open(M, 7, 1))

    def test_a_two_fork_flip_leaves_the_orders_with_the_round_that_won(self):
        # R1 (released) lands on fork B while R2 (carrying R1's orders + a fresh one) is in
        # flight; then fork A wins, where R2 landed instead. R2 must settle ALL its orders.
        # The old code stripped R1's orders from R2's record on the first sighting and put
        # them back only into the mempool: R2 then receipted just the fresh order, and the
        # next build "rejected" the two that had filled.
        server.SETTLE_HOLD_SECS = 30
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))
        self.build()
        r2 = server._inflight[M]
        self.chain.mark(r1)                                  # fork B: R1 landed
        self.sweeps(0)
        self.assertEqual(set(r2["claimed"]), {(7, 1, None), (8, 2, None)})
        self.chain.unmark(r1)                                # fork A wins: R2 landed
        self.chain.land(r2)
        self.sweeps(2)
        self.assertEqual(r2["claimed"], {}, "R2 owns them again")
        self.assertEqual(self.pending_oids(), [], "nothing re-queued: R2 carries them")
        self.sweeps(40)
        self.assertNotIn(M, server._inflight)
        for acct in (7, 8):
            self.assertEqual(self.dispositions(acct), ["filled"])
        self.build()                                         # nothing left to reject
        self.assertNotIn("rejected", self.dispositions(7) + self.dispositions(8))

    def test_two_rounds_sharing_orders_both_read_as_landed_waits_for_one_view(self):
        # they can't both have landed (they share an order): reads straddled a re-org. The
        # live round must not settle — and receipt — orders the other round holds.
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))
        self.build()
        r2 = server._inflight[M]
        self.chain.mark(r1)
        self.chain.mark(r2)
        self.sweeps(0, 1, 2)
        self.assertIs(server._inflight.get(M), r2, "contested: not finalized")
        self.assertEqual(self.receipts(8), [])
        self.chain.unmark(r1)                                # one view: R2 won
        self.sweeps(3, 6)
        self.assertNotIn(M, server._inflight)
        self.assertEqual(self.dispositions(8), ["filled"])

    def test_an_unlanded_released_round_is_forgotten_after_the_watch(self):
        server.ZOMBIE_WATCH_SECS = 900
        self.crossing_pair()
        self.build()
        self.time_out()
        self.assertEqual(len(server._zombies[M]), 1)
        self.sweeps(server.ROUND_GATE_SECS + 800)
        self.assertEqual(len(server._zombies[M]), 1, "still watched inside the window")
        self.sweeps(server.ROUND_GATE_SECS + 902)
        self.assertNotIn(M, server._zombies, "forgotten after ZOMBIE_WATCH_SECS")

    def test_several_released_rounds_are_watched_at_once(self):
        # two different released rounds; the OLDER one lands late and is still finalized
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))
        self.build()
        self.time_out()
        self.assertEqual(len(server._zombies[M]), 2)
        self.chain.land(r1)
        self.sweeps(0, 2)
        self.assertEqual(self.dispositions(7), ["filled"])


class OneRecordPerRoundId(_Base):
    def test_an_unchanged_rebuild_is_the_same_round_and_settles_once(self):
        # R1 times out and is rebuilt from the same inputs: the same payload, the same id.
        # Two records with one id used to BOTH finalize when it landed (two receipts per
        # order; with two timeouts, three).
        self.chain.set_book(server.order_bytes(5, 99, server.SELL, S, 10 * S))  # a resting maker
        self.queue(order(7, 1, "buy", 1, 10, seq=15))
        self.build()
        r1 = self.time_out()
        self.build()
        r2 = server._inflight[M]
        self.assertEqual(r2["rid"], r1["rid"])
        self.assertEqual(self.sent[0], self.sent[1], "the same bytes went out again")
        self.assertNotIn(M, server._zombies, "the in-flight record owns the id")
        r3 = self.time_out()
        self.build()
        self.chain.land(server._inflight[M])
        self.sweeps(0, 2, 4)
        self.assertEqual(self.dispositions(7), ["filled"])
        self.assertEqual([r["oid"] for r in self.receipts(5)], [99], "the maker, once")
        self.assertIs(r3["rid"], r3["rid"])

    def test_a_retried_sealed_round_carries_its_remainder_once(self):
        # a sealed buy 250 vs a resting sell 10: partial fill, one carry credit on-chain.
        # With a duplicate record the remainder was re-sealed twice, one commit was refused,
        # and one of two same-oid copies could never become ready.
        self.chain.set_book(server.order_bytes(20, 1, server.SELL, S, 10 * S))
        o = self.sealed_buy(7, 50, 250)
        self.chain.kv[b"commits" + u32(M)] = server.commitment(o["reveal"]) + u32(7)
        self.queue(o)
        self.build()
        self.time_out()
        self.build()
        self.chain.land(server._inflight[M])
        self.sweeps(0, 2)
        carries = [p for p in self.sent if p[0] == server.TAG_CARRY_COMMIT]
        self.assertEqual(len(carries), 1)
        self.assertEqual([x["oid"] for x in server.pending[M]], [50], "one remainder")


class NoInterleavingWithTheResolver(_Base):
    """The resolver thread and a round build used to touch the same market at once: a
    released round seen landing by the resolver while its orders sat only in a build's
    locals was receipted "rejected" by the build and "filled" by the resolver. They now
    hold the market's lock; the resolver skips a market being built."""

    def _resolver_now(self):
        th = threading.Thread(target=server._resolve_rounds_once, kwargs={"now": time.time()})
        th.start()
        th.join(5)
        self.assertFalse(th.is_alive(), "the resolver never blocks on a busy market")

    def test_landing_during_the_floor_reads(self):
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        real_get, fired = self.chain.get, []

        def hooked(key):
            if key == b"sq" + u32(7) and not fired:        # _seq_sanitize reads acct 7's floor
                fired.append(1)
                self.chain.land(r1)                        # R1 lands now ...
                self._resolver_now()                       # ... and the resolver sweeps
            return real_get(key)
        server.storage = hooked
        self.build()
        server.storage = real_get
        self.assertTrue(fired)
        self.assertEqual(self.receipts(7), [], "not rejected by the build")
        self.sweeps(2)
        for acct in (7, 8):
            self.assertEqual(self.dispositions(acct), ["filled"])

    def test_landing_during_the_submit(self):
        self.crossing_pair()
        self.build()
        r1 = self.time_out()
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))       # R2 = R1's orders + a fresh one

        def submit(payload, check=None, detail=""):
            self.sent.append(payload)
            self.chain.land(r1)
            self._resolver_now()
        server.submit = submit
        self.build()
        self.sweeps(0, server.DEAD_CONFIRM_SECS)
        for acct in (7, 8):
            self.assertEqual(self.dispositions(acct), ["filled"])
        self.assertEqual(self.pending_oids(), [3])
        self.build()                                         # the fresh order goes again, alone
        self.assertEqual([o["oid"] for o in server._inflight[M]["public"]], [3])
        self.assertNotIn("rejected", self.dispositions(7) + self.dispositions(8))


class SubmitOutcomeUnknown(_Base):
    def test_a_submit_timeout_keeps_the_round_in_flight(self):
        # the builder didn't answer: the payload may still reach the chain. The round used to
        # vanish (its orders in neither the mempool nor a round, never receipted).
        def timeout(payload, check=None, detail=""):
            raise TimeoutError("builder /submit timed out")
        server.submit = timeout
        self.crossing_pair()
        self.build()
        self.assertIn(M, server._inflight)
        self.assertEqual(self.pending_oids(), [])
        self.chain.land(server._inflight[M])
        self.sweeps(0, 2)
        self.assertEqual(self.dispositions(7), ["filled"])

    def test_a_carry_post_failure_does_not_abort_the_receipts(self):
        # a sealed partial fill whose carry re-seal hits a builder timeout: the receipts are
        # still written, the remainder queued for retry — with the SAME commitment
        self.chain.set_book(server.order_bytes(20, 1, server.SELL, S, 10 * S))
        o = self.sealed_buy(7, 50, 250)
        self.chain.kv[b"commits" + u32(M)] = server.commitment(o["reveal"]) + u32(7)
        self.queue(o)
        self.build()
        self.chain.land(server._inflight[M])
        posts = []

        def flaky(payload, check=None, detail=""):
            if payload[0] == server.TAG_CARRY_COMMIT:
                posts.append(payload)
                if len(posts) == 1:
                    raise TimeoutError("builder /submit timed out")
            self.sent.append(payload)
        server.submit = flaky
        self.sweeps(0, 2)
        self.assertEqual(self.dispositions(7), ["partial-carried"])
        self.assertEqual(len(server._carry_retry[M]), 1)
        self.sweeps(4)                                       # the retry goes through
        self.assertEqual(len(posts), 2)
        self.assertEqual(posts[0], posts[1], "the retry re-posts the same commitment")
        self.assertEqual([x["oid"] for x in server.pending[M]], [50])


class ExpiryPrunes(_Base):
    def test_a_released_prune_is_pruned_again_and_ends_only_when_it_lands(self):
        server.expired_pairs = self._saved["expired_pairs"]   # the real one
        x = server.order_bytes(5, 99, server.SELL, 2 * S, S)
        self.chain.set_book(x)
        server.order_expiry[(M, 5, 99)] = time.time() - 1
        order_telemetry.placed(M, 5, 99, server.SELL, 2 * S, S, False, False)
        self.build()                                         # a prune-only round
        r1 = server._inflight[M]
        self.assertEqual(r1["pruned"], [(5, 99)])
        self.assertTrue(order_telemetry.is_open(M, 5, 99), "not ended at build time")
        self.chain.set_book(x + server.order_bytes(6, 98, server.BUY, S, S))   # book moved
        self.sweeps(0, server.DEAD_CONFIRM_SECS)
        self.assertIn((M, 5, 99), server.order_expiry, "a released prune keeps its expiry")
        self.assertTrue(server.has_expired(M))
        self.build()                                         # ... so the next round prunes again
        r2 = server._inflight[M]
        self.assertEqual(r2["pruned"], [(5, 99)])
        self.chain.land(r2)
        self.sweeps(0, 2)
        self.assertFalse(order_telemetry.is_open(M, 5, 99), "ended once the prune landed")
        self.assertNotIn((M, 5, 99), server.order_expiry)


class NoOvertakingUnderTheCap(_Base):
    def test_an_accounts_older_orders_are_batched_first(self):
        server.MAX_ROUND_ORDERS = 4
        # queue order as a requeue-to-front + new arrivals can leave it: acct 7's seq 5
        # sits BEHIND its seq 30/31. A plain [:4] would batch 30 and 31, whose settling
        # strands seq 5 below acct 7's floor forever.
        self.queue(order(7, 1, "buy", 0.5, 1, seq=30), order(7, 2, "buy", 0.5, 1, seq=31),
                   order(8, 3, "sell", 2, 1, seq=1), order(8, 4, "sell", 2, 1, seq=2),
                   order(7, 5, "buy", 0.5, 1, seq=5), order(9, 6, "sell", 2, 1, seq=1))
        self.build()
        batch = server._inflight[M]["public"]
        held = server.pending[M]
        for acct in (7, 8, 9):
            got = [o["seq"] for o in batch if o["account"] == acct]
            wait = [o["seq"] for o in held if o["account"] == acct]
            if got and wait:
                self.assertLess(max(got), min(wait), f"acct {acct}: newer batched before older")
        self.assertEqual(sorted(o["seq"] for o in batch if o["account"] == 7), [5, 30])

    def test_cap_batch_keeps_positions_and_leaves_sealed_alone(self):
        sealed = {"account": 7, "oid": 9, "sealed": True, "seq": 0}
        orders = [order(7, 1, "buy", 1, 1, seq=9), sealed, order(7, 2, "buy", 1, 1, seq=3)]
        batch, rest = server._cap_batch(orders, 2)
        self.assertEqual([o["oid"] for o in batch], [2, 9])
        self.assertEqual([o["oid"] for o in rest], [1])

    def test_chain_busy_requeues_to_the_front(self):
        def busy(payload, check=None, detail=""):
            raise server.ChainBusy("all guarantors refused")
        server.submit = busy
        self.crossing_pair()
        self.build()
        self.assertNotIn(M, server._inflight)
        self.queue(order(9, 3, "buy", 0.5, 1, seq=1))
        self.assertEqual(self.pending_oids(), [1, 2, 3])


class RoundsTheServiceWouldRefuse(_Base):
    """An order the service refuses sinks its WHOLE round, and _round_dead can't see why, so
    front-requeue then kept it in every round: a permanent wedge. The builder now drops it."""

    def test_a_repeated_seq_is_dropped_with_a_receipt(self):
        server.MAX_ROUND_ORDERS = 3
        self.queue(order(7, 1, "buy", 1, 10, seq=15), order(7, 2, "buy", 1, 10, seq=15),
                   order(8, 3, "sell", 1, 10, seq=3), order(9, 4, "sell", 2, 1, seq=1))
        self.build()
        self.assertEqual([(o["account"], o["seq"]) for o in server._inflight[M]["public"]],
                         [(7, 15), (8, 3)])
        rec = self.receipts(7)
        self.assertEqual((rec[0]["disposition"], rec[0]["oid"]), ("rejected", 2))
        self.assertTrue(rec[0]["reason"].startswith("duplicate"))

    def test_a_market_order_is_priced_inside_the_band_of_the_current_last_price(self):
        self.lp = 1_000_007                                 # moved since placement
        self.queue(order(7, 1, "buy", 1, 10, seq=15, otype="market"))
        self.build()
        o = server._inflight[M]["public"][0]
        self.assertEqual(o["price"], 1_100_007)
        self.assertLessEqual(o["price"] * 100, self.lp * 110)
        self.assertGreaterEqual(o["price"] * 100, self.lp * 90)

    def test_a_market_order_with_no_last_price_is_dropped(self):
        self.queue(order(7, 1, "buy", 1, 10, seq=15, otype="market"),
                   order(8, 2, "sell", 1, 10, seq=3))
        self.build()
        self.assertEqual([o["oid"] for o in server._inflight[M]["public"]], [2])
        self.assertIn("no last price", self.receipts(7)[0]["reason"])

    def test_market_prices_always_pass_the_services_integer_band_check(self):
        # service floors::check_bindings: accepts p iff lp*90 <= p*100 <= lp*110. The old
        # round(lp*1.1) / round(lp*0.9) failed that for ~45% of prices.
        for lp in list(range(1, 2000)) + list(range(1_000_000, 1_001_000)):
            for side in (server.BUY, server.SELL):
                p = server.market_price(side, lp)
                self.assertTrue(lp * 90 <= p * 100 <= lp * 110, (lp, side, p))


class OrderDoorChecks(_Base):
    def setUp(self):
        super().setUp()
        server.pubkey_of_handle = lambda h: PK
        server.MAX_OPEN_ORDERS = 0

    def _post(self, seq):
        return server.api_order({"market": M, "side": "buy", "qty": 1, "price": 1,
                                 "account": 7, "seq": seq, "sig": "00" * 64})

    def test_a_reposted_order_is_refused(self):
        self._post(15)
        with self.assertRaisesRegex(ValueError, "already used"):
            self._post(15)
        self.assertEqual(len(server.pending[M]), 1)

    def test_a_seq_at_or_below_the_settled_floor_is_refused(self):
        self.chain.set_floor(b"sq", 7, 20)
        with self.assertRaisesRegex(ValueError, "not above"):
            self._post(20)
        self._post(21)


class TruthfulDeadOrderReceipts(_Base):
    def test_a_superseded_order_ends_with_a_reason(self):
        self.chain.set_floor(b"sq", 7, 20)                   # acct 7 settled seq 20 already
        self.crossing_pair()
        self.build()
        self.assertEqual([o["oid"] for o in server._inflight[M]["public"]], [2],
                         "the stale order is not submitted (it would sink the round)")
        rec = self.receipts(7)
        self.assertEqual(len(rec), 1)
        self.assertEqual(rec[0]["disposition"], "rejected")
        self.assertEqual(rec[0]["oid"], 1)
        self.assertTrue(rec[0]["reason"].startswith("superseded: account seq floor"))
        self.assertIn("floor 20 >= order seq 15", rec[0]["reason"])
        self.assertFalse(order_telemetry.is_open(M, 7, 1), "terminal in the telemetry too")
        self.assertNotIn((M, 7, 1), server.order_expiry)


class EncryptUntilBatch(_Base):
    def setUp(self):
        super().setUp()
        server.ENC_MODE = True
        self.rounds = []

        def committee(cmd, *args):
            if cmd == "encrypt":                             # (market, order hex, seed)
                return {"ciphertext": (b"\x02" * 32 + bytes.fromhex(args[1])).hex()}
            if cmd == "round":                               # (m, base, quote, section, cts)
                payload = bytes([server.TAG_ENC_ROUND]) + b"committee-proofs" + bytes.fromhex(args[3])
                self.rounds.append(payload)
                return {"round": payload.hex()}
            raise AssertionError(cmd)
        server.committee_run = committee
        self.chain.set_book(server.order_bytes(20, 1, server.SELL, S, 10 * S))
        self.o = self.sealed_buy(7, 50, 10)

    def test_a_reveal_waits_for_its_enc_commit(self):
        # ungated, the round was built before the ENC_COMMIT landed, judged "commit-gone",
        # released without cooldown and rebuilt (committee re-run, re-submitted) every auction
        self.queue(self.o)
        self.build()
        self.assertEqual((self.sent, self.rounds), ([], []), "deferred, nothing submitted")
        self.assertEqual(self.pending_oids(), [50])
        self.chain.kv[b"encset" + u32(M)] = server._consumed_entry(self.o)
        self.build()
        self.assertEqual(len(self.rounds), 1)

    def test_the_tracked_id_is_the_id_of_the_committee_payload(self):
        self.chain.kv[b"encset" + u32(M)] = server._consumed_entry(self.o)
        self.queue(self.o)
        self.build()
        fr, payload = server._inflight[M], self.rounds[0]
        self.assertEqual(self.sent, [payload], "submitted exactly the committee's bytes")
        self.assertEqual(fr["rid"], hashlib.blake2s(b"jamswap:v1:round" + payload,
                                                    digest_size=32).digest())
        self.assertEqual(fr["set_key"], b"encset")
        self.assertEqual(fr["consumed"], [server.commitment(bytes.fromhex(self.o["ciphertext"]))
                                          + u32(7)])


class CancelsEndTheLifecycle(_Base):
    def _resting(self, acct, oid):
        self.chain.set_book(server.order_bytes(acct, oid, server.BUY, S, 5 * S))
        order_telemetry.placed(M, acct, oid, server.BUY, S, 5 * S, False, False)

    def _cancel(self, acct, oid, nonce=0):
        server.api_cancel({"account": acct, "market": M, "order_id": oid, "nonce": nonce,
                           "sig": "00" * 64})

    def test_a_landed_signed_cancel_records_the_terminal(self):
        self._resting(7, 42)
        self._cancel(7, 42)
        self.assertEqual(len(self.sent), 1)
        server.nonce_of = lambda h: 0
        server._resolve_rounds_once(now=time.time())
        self.assertTrue(order_telemetry.is_open(M, 7, 42), "not landed yet")
        server.nonce_of = lambda h: 1                         # the cancel accumulated...
        self.chain.set_book(b"")                              # ...and removed the order
        server._resolve_rounds_once(now=time.time())
        self.assertFalse(order_telemetry.is_open(M, 7, 42))
        rec = self.receipts(7)[0]
        self.assertEqual((rec["disposition"], rec["oid"], rec["qty"]), ("cancelled", 42, 5))
        self.assertEqual(server._cancel_watch, [])

    def test_a_cancel_that_removed_nothing_ends_nothing(self):
        self._resting(7, 42)
        self._cancel(7, 42)
        server.nonce_of = lambda h: 1                         # nonce used, order still resting
        server._resolve_rounds_once(now=time.time())
        self.assertTrue(order_telemetry.is_open(M, 7, 42))
        self.assertEqual(self.receipts(7), [])

    def test_a_landed_round_that_filled_it_is_resolved_first(self):
        server.SETTLE_HOLD_SECS = 30
        self._resting(7, 42)
        self._cancel(7, 42)
        self.queue(order(8, 2, "sell", 1, 5, seq=1))          # crosses the resting buy
        self.build()
        fr = server._inflight[M]
        self.assertTrue(fr["clearing"]["fills"].get(42))
        self.chain.land(fr)                                   # the round won the race ...
        server.nonce_of = lambda h: 1                         # ... the cancel found nothing
        self.chain.set_book(b"")
        self.sweeps(0)
        self.assertEqual(len(server._cancel_watch), 1, "waits: that round's receipt ends it")
        self.sweeps(40)                                       # the round finalizes: filled
        self.sweeps(41)
        self.assertEqual(self.dispositions(7), ["filled"], "one terminal, not a cancel too")
        self.assertEqual(server._cancel_watch, [])

    def test_an_unlanded_round_that_would_fill_it_does_not_hold_the_cancel(self):
        # the cancel took the order off the book, so a round built on the old book can never
        # land; the watch used to wait on it until it lapsed, leaving the order "open" forever
        self._resting(7, 42)
        self._cancel(7, 42)
        self.queue(order(8, 2, "sell", 1, 5, seq=1))
        self.build()
        self.time_out()                                       # released, still watched
        server.nonce_of = lambda h: 1
        self.chain.set_book(b"")
        self.sweeps(server.ROUND_GATE_SECS + 2)
        self.assertEqual(self.dispositions(7), ["cancelled"])
        self.assertEqual(server._cancel_watch, [])

    def test_a_cancel_appended_while_the_watch_is_processed_is_kept(self):
        self.chain.set_book(server.order_bytes(7, 42, server.BUY, S, 5 * S)
                            + server.order_bytes(7, 43, server.BUY, S, 5 * S))
        self._cancel(7, 42)
        calls = []

        def nonce(h):
            if not calls:
                calls.append(1)
                self._cancel(7, 43, nonce=1)                  # an HTTP thread, mid-sweep
            return 0
        server.nonce_of = nonce
        server._resolve_cancels(time.time())
        self.assertEqual(sorted(c["oid"] for c in server._cancel_watch), [42, 43])

    def test_cancel_pending_records_the_terminal(self):
        self.queue(order(7, 1, "buy", 1, 10, seq=15))
        r = server.api_cancel_pending({"account": 7, "order_id": 1})
        self.assertEqual(r["removed"], 1)
        self.assertFalse(order_telemetry.is_open(M, 7, 1))
        self.assertEqual(self.dispositions(7), ["cancelled"])

    def test_cancel_pending_waits_while_its_released_round_can_still_land(self):
        # the order is back in the mempool because its round timed out — but that round may
        # still land (then the order traded). A "cancelled" receipt then was false.
        self.crossing_pair()
        self.build()
        self.time_out()
        with self.assertRaisesRegex(ValueError, "may still settle"):
            server.api_cancel_pending({"account": 7, "order_id": 1})
        self.assertEqual(self.pending_oids(), [1, 2])
        self.chain.set_book(server.order_bytes(5, 99, server.SELL, 2 * S, S))  # can't land now
        r = server.api_cancel_pending({"account": 7, "order_id": 1})
        self.assertEqual(r["removed"], 1)
        self.assertEqual(self.dispositions(7), ["cancelled"])


class RoundIdMatchesTheService(_Base):
    # crates/match-engine/src/round_id.rs `fixture_id_matches_the_server` asserts this id too
    RUST_FIXTURE_ID = "097a0379d26b1553e50fcb50e9e29024d65bf520be3ee4243f69c8660a3f5e3d"

    def test_shared_fixture(self):
        payload = b"\x0c\x01\x00\x00\x00jamswap round fixture"
        self.assertEqual(server.round_id(payload).hex(), self.RUST_FIXTURE_ID)

    def test_the_tracked_id_is_over_the_exact_submitted_payload(self):
        self.chain.set_book(server.order_bytes(5, 99, server.SELL, 2 * S, S))
        self.crossing_pair()
        self.build()
        self.assertEqual(self.sent[0][0], server.TAG_SMATCH)
        self.assertEqual(server._inflight[M]["rid"], server.round_id(self.sent[0]))

    def test_the_id_binds_the_prune_list(self):
        # a prune-only round and the no-op on the same book used to share one id, so anyone's
        # no-op landing "settled" the builder's prune round
        book = server.order_bytes(5, 99, server.SELL, 2 * S, S)
        self.chain.set_book(book)
        server.expired_pairs = lambda m, raw: [(5, 99)]
        self.build()
        hdr = bytes([server.TAG_SMATCH]) + struct.pack("<III", M, 1, 0)
        noop = hdr + server.public_section_bytes([], [], book)
        self.assertEqual(self.sent[0], hdr + server.public_section_bytes([], [(5, 99)], book))
        self.assertNotEqual(server._inflight[M]["rid"], server.round_id(noop))


if __name__ == "__main__":
    unittest.main(verbosity=2)
