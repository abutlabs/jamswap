"""Finality from the chain (#12): durable decisions read the state at the finalized head.

  * jip2 (reads at "final"): a sealed commit may be revealed once it is in the commit set
    at the finalized head; a round is receipted once its landed marker is there, and while
    finality advances that is the only way in (the derived settle hold is 0: no timer); a
    stalled finality falls back to the slot-counted hold, and a fill receipted that way
    turns final once its round reaches the finalized state.
  * The derived settle hold: 0 while the finalized head moves within the window,
    SETTLE_HOLD_SLOTS slots when it does not (or there is no finality), the
    SETTLE_HOLD_SECS override whatever finality does.
  * jamnp (cannot read at "final"; lasair#70) keeps its height rules. The reveal gate and
    the settle hold are compared with the pre-change code (copied below from e00b76e as a
    reference oracle, as test_chain.py does for the adapter), and its receipts gain no field.

Run with:  python3 -m unittest discover -s offchain/tests
"""
import importlib.util
import os
import struct
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import chain             # noqa: E402
import order_telemetry   # noqa: E402
from chain import Block, ChainUnsupported   # noqa: E402
from test_chain import FakeBridge           # noqa: E402

SID, M = 100, 1
S = 10_000
PK = bytes([0x11]) * 32


def u32(x):
    return struct.pack("<I", x)


def h32(n):
    return bytes([n]) * 32


class FinalChain(chain.Chain):
    """A backend that reads at the best and at the finalized head, like jip2: two stores and
    two block descriptors. A read at a Block reads the store of the head it names."""
    name, submits = "fake-jip2", True

    def __init__(self):
        super().__init__(SID)
        self.best_kv, self.final_kv = {}, {}
        self.best, self.final = Block(50, h32(0xB0), None), Block(47, h32(0xF0), None)
        self.reads = []

    def head(self):
        return self.best

    def finalized(self):
        return self.final

    def read(self, key, at="best"):
        self.reads.append((bytes(key), at))
        if at == "final" or (isinstance(at, Block) and at.hash == self.final.hash):
            return self.final_kv.get(bytes(key), b"")
        if at == "best" or (isinstance(at, Block) and at.hash == self.best.hash):
            return self.best_kv.get(bytes(key), b"")
        raise AssertionError(f"read at an unknown head {at!r}")

    def finalize(self, slot=None):
        # the finalized head moves: a new block (new hash) at a later slot
        n = (self.final.slot + 1) if slot is None else slot
        self.final = Block(n, h32(n & 0xFF), None)
        if self.best.slot < n:
            self.best = Block(n, h32((n + 0x80) & 0xFF), None)


def fresh_server(backend):
    # a private copy of server.py bound to `backend` (no state shared with other tests)
    spec = importlib.util.spec_from_file_location("server_finality_under_test",
                                                  os.path.join(HERE, "..", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.CHAIN = backend
    return mod


def order(acct, oid, side, price, qty, seq):
    # a signed public order as api_order queues it (signature framed, never verified here)
    return {"account": acct, "oid": oid, "side": 0 if side == "buy" else 1,
            "price": int(price * S), "qty": int(qty * S), "sealed": False, "address": "",
            "type": "limit", "signed_price": int(price * S), "seq": seq, "pubkey": PK,
            "sig": bytes(64)}


class _Server(unittest.TestCase):
    """server.py on a fake backend, with its chain-writing side effects stubbed."""
    backend = FinalChain

    def setUp(self):
        self.c = self.backend()
        self.srv = srv = fresh_server(self.c)
        tmp = tempfile.mkdtemp()
        srv.EXECS_FILE = os.path.join(tmp, "execs.json")
        self._events = order_telemetry.ORDER_EVENTS_FILE
        order_telemetry.ORDER_EVENTS_FILE = os.path.join(tmp, "events.jsonl")
        self.sent = []
        srv.submit = lambda payload, check=None, detail="": self.sent.append(payload)
        srv.signer_key = lambda h: PK
        srv.REQUIRE_ORDER_SIG = False
        srv.ENC_MODE = False
        srv.JAMKB_BACKPRESSURE = False
        srv.SETTLE_HOLD_SECS = None          # derived, whatever the test environment says
        srv.SETTLE_HOLD_SLOTS = 25
        srv.FINALITY_WINDOW_SLOTS = 10

    def tearDown(self):
        order_telemetry.ORDER_EVENTS_FILE = self._events

    # finality over (synthetic) time ------------------------------------------------
    def look(self, now, move=False):
        # the server looks at the chain at `now`; `move` finalizes one more block first
        if move:
            self.c.finalize()
        self.srv._fin_cache["v"] = None
        return self.srv._read_finality(now)

    def advancing(self, t0):
        # a finality seen to move just before t0
        self.look(t0 - 4)
        self.look(t0 - 2, move=True)

    # rounds -------------------------------------------------------------------------
    def crossing_round(self):
        self.srv.pending.setdefault(M, []).extend(
            [order(7, 1, "buy", 1, 10, seq=1), order(8, 2, "sell", 1, 10, seq=1)])
        r = self.srv.api_round({"market": M, "base": 1, "quote": 0})
        self.assertTrue(r.get("queued"), r)
        return self.srv._inflight[M]

    def land(self, fr, final=False, slot=48):
        self.c.best_kv[b"rl" + fr["rid"]] = u32(slot)
        if final:
            self.c.final_kv[b"rl" + fr["rid"]] = u32(slot)

    def sweep(self, now, move=True):
        # one resolver sweep at `now`, the finalized head moving first unless `move` is False
        if move:
            self.c.finalize()
        self.srv._fin_cache["v"] = None
        self.srv._resolve_rounds_once(now=now)

    def receipts(self, acct):
        return self.srv.api_executions({"account": acct})["executions"]


class DerivedSettleHold(_Server):
    def test_zero_while_finality_advances(self):
        t = time.time()
        self.advancing(t)
        self.assertEqual(self.srv.settle_hold_secs(t), 0.0)

    def test_slot_counted_when_finality_stalls(self):
        t = time.time()
        self.advancing(t)
        # nothing moves for more than FINALITY_WINDOW_SLOTS slots (60 s)
        self.assertEqual(self.srv.settle_hold_secs(t + 50), 0.0)
        self.assertEqual(self.srv.settle_hold_secs(t + 61), 25 * 6.0)
        self.srv.SETTLE_HOLD_SLOTS = 3
        self.assertEqual(self.srv.settle_hold_secs(t + 61), 18.0)
        self.look(t + 64, move=True)                          # finality resumes
        self.assertEqual(self.srv.settle_hold_secs(t + 64), 0.0)

    def test_slot_counted_without_finality(self):
        self.c.final = Block(0, h32(0), None)                 # only genesis is final
        t = time.time()
        self.advancing(t)
        self.assertEqual(self.srv.settle_hold_secs(t), 150.0)
        self.c.finalized = lambda: None                        # a backend with no finality at all
        self.srv._fin_cache["v"] = None
        self.assertEqual(self.srv.settle_hold_secs(t + 3), 150.0)

    def test_a_look_after_a_long_silence_is_not_advancing(self):
        # the finalized head moved at some moment in a 5-minute gap between looks: that
        # proves nothing about the last minute, so it does not count as advancing
        t = time.time()
        self.look(t)
        self.look(t + 300, move=True)
        self.assertEqual(self.srv.settle_hold_secs(t + 300), 150.0)
        self.look(t + 303, move=True)                          # a move seen within the window
        self.assertEqual(self.srv.settle_hold_secs(t + 303), 0.0)

    def test_the_override_wins(self):
        t = time.time()
        self.advancing(t)
        self.srv.SETTLE_HOLD_SECS = 42.0
        self.assertEqual(self.srv.settle_hold_secs(t), 42.0)
        self.srv.SETTLE_HOLD_SECS = 0.0
        self.assertEqual(self.srv.settle_hold_secs(t + 500), 0.0)

    def test_env(self):
        old = {k: os.environ.get(k) for k in ("SETTLE_HOLD_SECS", "SETTLE_HOLD_SLOTS")}
        try:
            os.environ.pop("SETTLE_HOLD_SECS", None)
            os.environ["SETTLE_HOLD_SLOTS"] = "4"
            srv = fresh_server(self.c)
            self.assertIsNone(srv.SETTLE_HOLD_SECS)
            self.assertEqual(srv.settle_hold_secs(), 24.0)     # no movement seen yet
            os.environ["SETTLE_HOLD_SECS"] = "0"
            self.assertEqual(fresh_server(self.c).settle_hold_secs(), 0.0)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class RoundsReceiptAtTheFinalizedHead(_Server):
    def test_a_landing_only_on_the_best_chain_waits_for_finality(self):
        t = time.time()
        self.advancing(t)
        fr = self.crossing_round()
        self.land(fr)                                          # at best, not yet final
        for dt in range(0, 201, 3):                            # finality keeps advancing ...
            self.sweep(t + dt)                                 # (the resolver sweeps every 2 s)
        self.assertIs(self.srv._inflight.get(M), fr, "no timer: finality has not reached it")
        self.assertEqual(self.receipts(7), [])
        self.land(fr, final=True)                              # ... and reaches it
        self.sweep(t + 203)
        self.assertNotIn(M, self.srv._inflight)
        self.assertEqual([r["disposition"] for r in self.receipts(7)], ["filled"])

    def test_a_landing_in_the_finalized_state_is_receipted_at_the_next_sweep(self):
        t = time.time()
        self.advancing(t)
        fr = self.crossing_round()
        self.land(fr, final=True)
        self.sweep(t)                                          # first sighting
        self.assertIn(M, self.srv._inflight)
        self.sweep(t + 2.5)                                    # durable: no hold to wait out
        self.assertNotIn(M, self.srv._inflight)
        for acct in (7, 8):
            (r,) = self.receipts(acct)
            self.assertEqual((r["disposition"], r["final"], r["round"]),
                             ("filled", True, fr["rid"].hex()))
        self.assertIn((b"rl" + fr["rid"], "final"), self.c.reads)

    def test_stalled_finality_falls_back_to_the_slot_hold(self):
        t = time.time()
        self.advancing(t - 100)                                # last moved long ago
        fr = self.crossing_round()
        self.land(fr)
        self.sweep(t, move=False)
        self.sweep(t + 100, move=False)
        self.assertIn(M, self.srv._inflight, "held for SETTLE_HOLD_SLOTS slots")
        self.sweep(t + 151, move=False)
        self.assertNotIn(M, self.srv._inflight)
        (r,) = self.receipts(7)
        self.assertEqual((r["disposition"], r["final"]), ("filled", False),
                         "receipted on the hold: settled, not final")
        self.land(fr, final=True)                              # finality catches up
        (r,) = self.receipts(7)
        self.assertTrue(r["final"])
        n = len(self.c.reads)
        self.receipts(7)
        self.assertEqual(len(self.c.reads), n, "a final receipt is never read again")

    def test_an_override_is_the_best_chain_hold_and_finality_still_wins(self):
        self.srv.SETTLE_HOLD_SECS = 30.0
        t = time.time()
        self.advancing(t)
        fr = self.crossing_round()
        self.land(fr)                                          # at best only
        self.sweep(t)
        self.sweep(t + 3)
        self.assertIn(M, self.srv._inflight, "the override's hold runs")
        self.sweep(t + 31)
        self.assertNotIn(M, self.srv._inflight, "no wait for finality under an override")
        self.assertEqual([r["final"] for r in self.receipts(7)], [False])
        self.srv.pending[M] = [order(7, 3, "buy", 1, 10, seq=2), order(8, 4, "sell", 1, 10, seq=2)]
        self.srv._round_gate.clear()
        self.srv.api_round({"market": M, "base": 1, "quote": 0})
        fr2 = self.srv._inflight[M]
        self.land(fr2, final=True)                             # in the finalized state
        self.sweep(t + 40)
        self.sweep(t + 42.5)
        self.assertNotIn(M, self.srv._inflight, "a final landing needs no hold")

    def test_a_reorg_before_finality_holds_the_round(self):
        t = time.time()
        self.advancing(t)
        fr = self.crossing_round()
        self.land(fr)
        self.sweep(t)
        del self.c.best_kv[b"rl" + fr["rid"]]                  # the fork it landed on lost
        self.sweep(t + 3)
        self.assertIs(self.srv._inflight.get(M), fr)
        self.assertIsNone(fr.get("ok_since"))
        self.assertEqual(self.receipts(7), [])


class SealedRevealGate(_Server):
    def sealed_buy_and_sell(self, commit_best=True, commit_final=True):
        o = {"account": 7, "oid": 5, "side": 0, "price": 1 * S, "qty": 10 * S,
             "type": "limit", "sealed": True, "address": ""}
        o["commit"] = self.srv._seal_material(M, o)
        entry = self.srv.commitment(o["reveal"]) + u32(7)
        if commit_best:
            self.c.best_kv[b"commits" + u32(M)] = entry
        if commit_final:
            self.c.final_kv[b"commits" + u32(M)] = entry
        self.srv.pending.setdefault(M, []).extend([o, order(8, 2, "sell", 1, 10, seq=1)])
        self.srv.api_round({"market": M, "base": 1, "quote": 0})
        fr = self.srv._inflight.get(M)
        revealed = bool(fr) and any(x["oid"] == 5 for x in fr["sealed"])
        deferred = any(x["oid"] == 5 for x in self.srv.pending.get(M, []))
        self.assertNotEqual(revealed, deferred)
        return revealed

    def test_a_commit_in_the_finalized_state_is_revealed(self):
        # the commit landed at slot 45: it is in the state at the finalized head (slot 47),
        # though the head it was first seen at (slot 50) is not final yet — the height rule
        # would wait for that
        self.assertTrue(self.sealed_buy_and_sell())
        self.assertIn((b"commits" + u32(M), "final"), self.c.reads)

    def test_a_commit_not_in_the_finalized_state_waits_whatever_the_slots_say(self):
        # the finalized head is at the best head's slot, but on another fork: the commit is
        # not in its state, so it is not final (the height rule would reveal it)
        self.c.final = Block(50, h32(0xF1), None)
        self.assertFalse(self.sealed_buy_and_sell(commit_final=False))

    def test_a_commit_off_the_best_chain_is_never_revealed(self):
        self.assertFalse(self.sealed_buy_and_sell(commit_best=False, commit_final=True))

    def test_without_finality_best_chain_membership_reveals(self):
        self.c.final = Block(0, h32(0), None)                  # genesis only: no finality
        self.assertTrue(self.sealed_buy_and_sell(commit_final=False))
        self.assertNotIn((b"commits" + u32(M), "final"), self.c.reads)


# ---- jamnp: the pre-change rules, verbatim from server.py @ e00b76e (reference oracle) ----
def legacy_sealed_ready_predicate(_commit_seen, _consumed_entry, m, commit_entries, fin):
    seen = _commit_seen.setdefault(m, {})
    bh = fin.get("block_height")
    for e in commit_entries:                 # first sighting of a commit: pin the head height
        if e not in seen and bh is not None:
            seen[e] = bh
    for e in list(seen):                     # forget commits that left the set (consumed/expired)
        if e not in commit_entries:
            del seen[e]
    fh = fin.get("finalized_height") if fin.get("available") else None
    def ready(o):
        e = _consumed_entry(o)
        if e not in commit_entries:
            return False                     # not on-chain yet — defer
        if fh is None:
            return True                      # non-finalizing chain: best-chain membership is all we have
        h = seen.get(e)
        return h is not None and fh >= h     # β-finalized: durable, safe to reveal
    return ready


def legacy_confirmed(env_hold, ok_since, now):
    SETTLE_HOLD_SECS = float(env_hold if env_hold is not None else "150")
    MIN_CONFIRM_SECS = 2.0
    return now - ok_since >= max(SETTLE_HOLD_SECS, MIN_CONFIRM_SECS)


GAUGES = "lasair_block_height {bh}\nlasair_finalized_height {fh}\nlasair_slot {s}\nlasair_finalized_slot {fs}\n"


class JamnpKeepsItsHeightRules(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.c = chain.JamnpChain(SID, self.bridge.url, self.bridge.url, self.bridge.url + "/metrics")
        self.srv = fresh_server(self.c)
        self.srv.ENC_MODE = False

    def tearDown(self):
        self.bridge.stop()

    def gauges(self, bh, fh):
        self.bridge.metrics = GAUGES.format(bh=bh, fh=fh, s=bh + 7000000, fs=fh + 7000000)
        self.c._scrape = (0.0, None)
        self.srv._fin_cache["v"] = None

    def test_no_read_at_the_finalized_head_and_no_request_for_one(self):
        self.assertIsNone(self.srv._storage_final(b"commits" + u32(M)))
        self.assertIsNone(self.srv._landed_final(h32(3)))
        self.assertEqual(self.bridge.requests, [])

    def test_reveal_gate_matches_the_pre_change_rule(self):
        orders = []
        for acct in (7, 8, 9):
            o = {"account": acct, "oid": acct, "side": 0, "price": S, "qty": S, "sealed": True}
            o["commit"] = self.srv._seal_material(M, o)
            orders.append(o)
        e = [self.srv._consumed_entry(o) for o in orders]
        # (commit set on the best chain, head height, finalized height) over time
        script = [({e[0]}, 100, 98), ({e[0], e[1]}, 101, 99), ({e[0], e[1]}, 102, 100),
                  ({e[1], e[2]}, 103, 101), ({e[1], e[2]}, 104, 0), ({e[1], e[2]}, 105, 103),
                  (set(), 106, 105), ({e[2]}, 107, 107)]
        legacy_seen = {}
        for entries, bh, fh in script:
            self.gauges(bh, fh)
            fin = self.srv._read_finality()
            # the call site: jamnp has no finalized-state read, so no final set is passed
            final_raw = self.srv._storage_final(b"commits" + u32(M)) if fin.get("available") else None
            self.assertIsNone(final_raw)
            new = self.srv._sealed_ready_predicate(M, entries, fin, None)
            old = legacy_sealed_ready_predicate(legacy_seen, self.srv._consumed_entry, M, entries, fin)
            self.assertEqual([new(o) for o in orders], [old(o) for o in orders], (entries, bh, fh))
            self.assertEqual(self.srv._commit_seen, legacy_seen)
        self.assertEqual({p for _m, p, _c, _b in self.bridge.requests}, {"/metrics"})

    def test_settle_hold_matches_the_pre_change_hold(self):
        # every setting the nets use, and finality advancing / stalled / absent. The one
        # change: with SETTLE_HOLD_SECS unset AND finality advancing the hold is now 0 (the
        # value lasair6, the one jamnp net that reports finality, already sets explicitly).
        t0 = time.time()
        rid = h32(9)
        for env in (None, "0", "18", "150"):
            for finality in ("advancing", "stalled", "absent"):
                srv = fresh_server(self.c)
                srv.SETTLE_HOLD_SECS = None if env is None else float(env)
                srv.SETTLE_HOLD_SLOTS, srv.FINALITY_WINDOW_SLOTS = 25, 10
                if finality == "absent":
                    self.c.metrics_url = ""
                else:
                    self.c.metrics_url = self.bridge.url + "/metrics"
                    self.gauges(100, 98)
                    srv._read_finality(t0 - 4)
                    self.gauges(101, 99 if finality == "advancing" else 98)
                    srv._fin_cache["v"] = None
                    srv._read_finality(t0 - 2)
                for elapsed in (0, 1.9, 2, 17, 18, 149, 150, 151):
                    now = t0 + elapsed
                    srv._fin_cache["v"] = None
                    if finality == "advancing":
                        srv._fin_track["moved"] = now - 2   # it keeps moving
                    elif finality == "stalled":
                        srv._fin_track["moved"] = t0 - 600
                    got = srv._confirmed({"rid": rid, "ok_since": t0}, now)
                    want = legacy_confirmed(env, t0, now)
                    if env is None and finality == "advancing":
                        want = legacy_confirmed("0", t0, now)
                    self.assertEqual(got, want, (env, finality, elapsed))
        self.assertFalse([r for r in self.bridge.requests if r[1] != "/metrics"],
                         "the hold asks nothing of the reader")

    def test_receipts_carry_no_new_field(self):
        srv = self.srv
        srv.EXECS_FILE = os.path.join(tempfile.mkdtemp(), "execs.json")
        events = order_telemetry.ORDER_EVENTS_FILE
        order_telemetry.ORDER_EVENTS_FILE = os.path.join(tempfile.mkdtemp(), "events.jsonl")
        try:
            self.gauges(120, 118)
            buy, sell = order(7, 1, "buy", 1, 10, 1), order(8, 2, "sell", 1, 10, 1)
            srv.record_executions(M, [], [], [buy, sell], rid=h32(4))
            srv.record_executions(M, [], [], [dict(buy, oid=3), dict(sell, oid=4)])  # the old call
            got = srv.api_executions({"account": 7})["executions"]
            self.assertEqual(len(got), 2)
            keys = {"ts", "market", "side", "price", "qty", "filled", "remainder",
                    "disposition", "reason", "oid", "settle_height"}
            for r in got:
                self.assertEqual(set(r), keys)
                self.assertEqual(r["settle_height"], 120)
            self.assertEqual({p for _m, p, _c, _b in self.bridge.requests}, {"/metrics"})
        finally:
            order_telemetry.ORDER_EVENTS_FILE = events


if __name__ == "__main__":
    unittest.main()
