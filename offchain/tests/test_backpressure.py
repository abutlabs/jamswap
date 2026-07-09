"""Backpressure tests — what the dex does when the chain refuses a submission.

When every lm node's CE-133 mempool is at cap (lasair --wp-queue-cap), the builder
reports {"accepted": false} and `server.submit` raises ChainBusy. The invariants:

  * a refused ROUND loses no orders — everything it tried to clear goes back into
    the mempool and batches into the retry;
  * the market cools down (zero-fill-style gate) instead of re-flooding the fleet;
  * NO settle predicate is installed for a round that never left the builder
    (a never-firing check would wedge the gate for ROUND_GATE_SECS);
  * the ledger entry resolves as "refused", never aging into a false settle-timeout.

All chain reads are monkeypatched; `server.py`'s startup is `__main__`-guarded, so
importing it is safe. Run with:  python3 -m unittest discover -s offchain/tests
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server   # noqa: E402
import metrics  # noqa: E402

M = 1


def public_order(oid, side="buy", price=100, qty=10):
    # carries the signed-order wire fields (signed_price/seq/pubkey/sig) so
    # public_section_bytes can serialize it; the signature is never verified
    # here (that's refine's job), only framed.
    return {"account": 7, "oid": oid, "side": server.BUY if side == "buy" else server.SELL,
            "price": price, "qty": qty, "sealed": False, "address": "", "type": "limit",
            "signed_price": price, "seq": oid, "pubkey": bytes(32), "sig": bytes(64)}


class RoundBackpressure(unittest.TestCase):
    def setUp(self):
        self._saved = {n: getattr(server, n)
                       for n in ("submit", "storage", "mstate", "expired_pairs")}
        server.storage = lambda key: b""            # empty on-chain book / sets
        server.mstate = lambda prefix, m: 0         # lp/cv all zero
        server.expired_pairs = lambda m, raw: []    # no GTT expiries
        server.pending.clear()
        server._round_gate.clear()
        server._inflight.clear()
        server.order_expiry.clear()

    def tearDown(self):
        for n, fn in self._saved.items():
            setattr(server, n, fn)
        server.pending.clear()
        server._round_gate.clear()
        server._inflight.clear()

    def test_refused_round_requeues_orders_and_cools_down(self):
        server.submit = self._raise_busy
        server.pending[M] = [public_order(1), public_order(2, side="sell")]
        r = server.api_round({"market": M, "base": 1, "quote": 2})
        self.assertFalse(r["ok"])
        self.assertTrue(r["backpressure"])
        self.assertEqual(r["requeued"], 2)
        # both orders are back in the mempool for the retry
        self.assertEqual(sorted(o["oid"] for o in server.pending[M]), [1, 2])
        # the market is cooling down: no settle predicate (nothing to settle), and
        # the next round attempt within the cooldown is gated
        self.assertIsNone(server._round_gate[M]["check"])
        r2 = server.api_round({"market": M, "base": 1, "quote": 2})
        self.assertTrue(r2.get("gated"), "cooldown must gate the immediate retry")
        # the re-queued orders are still there (the gated call consumed nothing)
        self.assertEqual(len(server.pending[M]), 2)

    def test_retry_after_cooldown_submits_the_batch(self):
        # first attempt refused, orders re-queued; once the chain accepts again the
        # SAME orders go on-chain in one batch.
        server.submit = self._raise_busy
        server.pending[M] = [public_order(1)]
        server.api_round({"market": M, "base": 1, "quote": 2})
        submitted = []
        server.submit = lambda payload, check=None, detail="": submitted.append(payload)
        server._round_gate[M]["t"] -= server.ROUND_ZEROFILL_SECS + 1   # cooldown elapsed
        r = server.api_round({"market": M, "base": 1, "quote": 2})
        self.assertTrue(r["ok"])
        self.assertEqual(len(submitted), 1, "the re-queued order went out in the retry")
        self.assertEqual(server.pending.get(M, []), [])

    def test_ledger_entry_resolves_as_refused(self):
        # a refused submission must not sit "pending" until the settle timeout
        tid = metrics.track("round", "test", check=lambda: False)
        metrics.refused(tid)
        entry = next(e for e in metrics.pending_snapshot() if e["id"] == tid)
        self.assertEqual(entry["state"], "refused")

    @staticmethod
    def _raise_busy(payload, check=None, detail=""):
        raise server.ChainBusy("all guarantors refused (CE-133 queues full)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
