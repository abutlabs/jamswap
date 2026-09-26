"""Orders left resting on the book are judged by the clearing price they met (jamswap#3).

The 2026-09-25 lasair6 soak scored three "misses" that were nothing of the kind: public
orders, marketable when placed, that lost their auction (the uniform price settled above
their limits) and rested on the on-chain book. The order telemetry had no `rested` state,
so they stayed "rounded" and soak_verdict counted them stuck open past the grace window —
and would have counted them missed again when they expired.

Now a settled round marks every public order it leaves on the book `rested`, with the
clearing price and whether the order's limit reached it (`crossed`). The verdict judges by
that instead of the placement-time guess: an outbid order rests (not a miss), clears if it
later fills, and is not a miss when it expires unfilled — unless it did cross a clearing
price. Sealed orders never rest publicly, so the zero-loss accounting is untouched.

Drives `server.py` against the fake chain of test_round_poison.py; the verdict half also runs
the real CLI on a synthetic event log. Run with:
    python3 -m unittest discover -s offchain/tests
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import server                                        # noqa: E402
import order_telemetry                               # noqa: E402
import soak_verdict as sv                            # noqa: E402
from test_round_poison import _Base, order, M, S, u32   # noqa: E402  (the fake-chain harness)

VERDICT = os.path.join(HERE, "..", "soak_verdict.py")


class _Resting(_Base):
    def setUp(self):
        super().setUp()
        fd, self.events_path = tempfile.mkstemp(prefix="jamswap_test_resting_", suffix=".jsonl")
        os.close(fd)
        self.addCleanup(os.remove, self.events_path)
        order_telemetry.ORDER_EVENTS_FILE = self.events_path   # _Base.tearDown restores it

    def place(self, o, marketable):
        server.pending.setdefault(M, []).append(o)
        order_telemetry.placed(M, o["account"], o["oid"], o["side"], o["price"], o["qty"],
                               o["sealed"], marketable)

    def settle(self):
        # the round in flight lands and holds: finalized (receipts, `rested` marks)
        fr = server._inflight[M]
        self.chain.land(fr)
        self.sweeps(0, 2)
        self.assertNotIn(M, server._inflight)
        return fr

    def book(self, *entries):
        # the market's on-chain book: (account, oid, side, atomic price, atomic qty) entries
        self.chain.set_book(b"".join(server.order_bytes(*e) for e in entries))

    def expire(self, acct, oid):
        # its good-till-time passes while it rests: the next round prunes it, and it ends
        # "expired" once that round lands (exactly the ExpiryPrunes path)
        server.expired_pairs = self._saved["expired_pairs"]
        server.order_expiry[(M, acct, oid)] = time.time() - 1
        self.build()
        self.settle()
        self.assertFalse(order_telemetry.is_open(M, acct, oid))

    def events(self, acct, oid):
        return sv.load(self.events_path)[(M, acct, oid)]

    def rests(self, acct, oid):
        return [(e["clearing"], e["crossed"], e["filled"])
                for e in self.events(acct, oid) if e["event"] == "rested"]

    def verdict(self, acct, oid):
        return sv.judge(self.events(acct, oid))

    def missed_live(self):
        return order_telemetry.snapshot()["missed"]

    def outbid_round(self):
        # a sell 8 @ 1.25 that both buys cross at placement; the buy @ 2 takes it all and the
        # uniform price settles at 2 — above the other buy's 1.50 limit (the soak's pattern)
        self.place(order(3, 1, "sell", 1.25, 8, seq=1), marketable=True)
        self.place(order(2, 2, "buy", 2, 8, seq=1), marketable=True)
        self.place(order(4, 3, "buy", 1.5, 11, seq=1), marketable=True)
        server.order_expiry[(M, 4, 3)] = time.time() + 3600
        self.build()
        fr = self.settle()
        self.assertEqual(fr["clearing"]["price"], 2 * S)
        self.assertEqual((self.dispositions(3), self.dispositions(2)), (["filled"], ["filled"]))
        return fr


class RestedOrders(_Resting):
    def test_an_order_that_loses_its_auction_rests_and_is_not_a_miss(self):
        self.outbid_round()
        self.assertTrue(order_telemetry.is_open(M, 4, 3), "live on the book")
        self.assertEqual(self.rests(4, 3), [(2 * S, False, 0)])
        v = self.verdict(4, 3)
        self.assertEqual((v["class"], v["marketable"]), ("resting", False))
        # resting long past the open grace is legitimate — until well past its own expiry
        # (then its prune never landed: stuck, like any order left open)
        orders, now = sv.load(self.events_path), time.time()
        r = sv.score(orders, now=now + 3000)
        self.assertEqual((r["stuck_open_count"], r["missed"], r["pass"]), (0, 0, True))
        r = sv.score(orders, now=now + 3600 + 700)
        self.assertEqual([k for k, _ in r["stuck_open"]], [(M, 4, 3)])
        # it expires resting without ever crossing a clearing price: it correctly sat there
        missed = self.missed_live()
        self.book((4, 3, server.BUY, int(1.5 * S), 11 * S))
        self.expire(4, 3)
        self.assertEqual(self.verdict(4, 3)["class"], "expired-nonmarketable")
        self.assertEqual(self.missed_live(), missed, "not a miss in the live SLO either")
        r = sv.score(sv.load(self.events_path), now=time.time() + 10 ** 6)
        self.assertEqual((r["cleared"], r["missed"], r["pass"]), (2, 0, True))

    def test_a_resting_order_that_later_fills_counts_as_cleared(self):
        self.outbid_round()
        # next round: a smaller sell meets it on the book — part of it trades, the rest rests
        self.book((4, 3, server.BUY, int(1.5 * S), 11 * S))
        self.place(order(5, 4, "sell", 1.5, 4, seq=1), marketable=True)
        self.build()
        self.settle()
        self.assertEqual(self.dispositions(4), ["partial-resting"])
        self.assertTrue(order_telemetry.is_open(M, 4, 3))
        self.assertEqual(self.rests(4, 3), [(2 * S, False, 0), (int(1.5 * S), True, 4 * S)])
        self.assertEqual(self.verdict(4, 3)["class"], "resting")
        # and the round after, the remainder fills
        self.book((4, 3, server.BUY, int(1.5 * S), 7 * S))
        self.place(order(5, 5, "sell", 1.25, 7, seq=2), marketable=True)
        self.build()
        self.settle()
        self.assertEqual(self.dispositions(4), ["filled", "partial-resting"])
        self.assertFalse(order_telemetry.is_open(M, 4, 3))
        self.assertEqual([e["event"] for e in self.events(4, 3)],
                         ["placed", "rounded", "rested", "rested", "terminal"])
        self.assertEqual(self.verdict(4, 3)["class"], "cleared")
        r = sv.score(sv.load(self.events_path), now=time.time() + 10 ** 6)
        self.assertEqual((r["cleared"], r["missed"], r["pass"]), (5, 0, True))

    def test_an_order_rationed_at_the_clearing_price_is_not_a_miss(self):
        # two buys @ 2 for one sell @ 1: the uniform price is 1 and time priority gives the
        # sell to the older buy. The younger one CROSSED the clearing price and got nothing:
        # rationed by price-time priority, an auction outcome, so it rests and even its
        # unfilled expiry is not counted against the chain.
        self.place(order(2, 1, "buy", 2, 1, seq=1), marketable=False)
        self.place(order(4, 2, "buy", 2, 1, seq=1), marketable=False)
        self.place(order(3, 3, "sell", 1, 1, seq=1), marketable=True)
        self.build()
        fr = self.settle()
        self.assertEqual((fr["clearing"]["price"], fr["clearing"]["fills"].get(2)), (S, None))
        self.assertEqual(self.rests(4, 2), [(S, True, 0)])
        v = self.verdict(4, 2)
        self.assertEqual((v["class"], v["marketable"], v["crossed"]), ("resting", False, True))
        missed = self.missed_live()
        self.book((4, 2, server.BUY, 2 * S, S))
        self.expire(4, 2)
        self.assertEqual(self.verdict(4, 2)["class"], "expired-nonmarketable")
        self.assertEqual(self.missed_live(), missed, "not a miss in the live SLO either")
        r = sv.score(sv.load(self.events_path), now=time.time() + 10 ** 6)
        self.assertEqual((r["missed"], r["sample_missed"]), (0, []))

    def test_sealed_orders_never_rest_publicly(self):
        # a revealed sealed order's remainder is re-sealed and carried, never put on the book:
        # no `rested` event, so the sealed zero-loss accounting is unchanged
        o = self.sealed_buy(7, 50, 10)
        self.chain.kv[b"commits" + u32(M)] = server.commitment(o["reveal"]) + u32(7)
        self.book((8, 2, server.SELL, S, 4 * S))
        order_telemetry.placed(M, 8, 2, server.SELL, S, 4 * S, False, False)
        self.place(o, marketable=False)
        self.build()
        self.settle()
        self.assertEqual(self.dispositions(7), ["partial-carried"])
        self.assertEqual(self.rests(7, 50), [])
        self.assertEqual(self.verdict(7, 50)["class"], "open", "still working, hidden")


def ev(ts, event, key, marketable, **kw):
    m, a, oid = key
    return dict(ts=round(ts, 3), event=event, market=m, account=a, oid=oid,
                marketable=marketable, **kw)


def lifecycle(key, t, marketable=True, sealed=False, rest=None, end=None):
    """One order's JSONL lines as order_telemetry writes them: placed, rounded, then an
    optional `rested` (dict: crossed, expires_at) and an optional terminal outcome."""
    out = [ev(t, "placed", key, marketable, side=0, price=15000, qty=100000, sealed=sealed),
           ev(t + 4, "rounded", key, marketable)]
    if rest is not None:
        out.append(ev(t + 20, "rested", key, rest["crossed"], clearing=20000,
                      crossed=rest["crossed"], filled=0, expires_at=rest.get("expires_at")))
        marketable = rest["crossed"]
    if end is not None:
        out.append(ev(t + 60, "terminal", key, marketable, outcome=end,
                      filled=100000 if end == "filled" else 0, retries=0, latency=60))
    return out


class VerdictCli(unittest.TestCase):
    """soak_verdict.py, end to end, on small synthetic event logs."""

    def run_verdict(self, lines):
        fd, path = tempfile.mkstemp(prefix="jamswap_test_verdict_", suffix=".jsonl")
        with os.fdopen(fd, "w") as fh:
            fh.write("".join(json.dumps(x) + "\n" for x in lines))
        self.addCleanup(os.remove, path)
        p = subprocess.run([sys.executable, VERDICT, path, "--json"],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, json.loads(p.stdout)

    def test_outbid_resting_and_expired_orders_pass(self):
        t, now = time.time() - 5000, time.time()          # everything is past the 600 s grace
        lines = (lifecycle((1, 2, 1), t, rest={"crossed": False, "expires_at": now + 3000})
                 + lifecycle((1, 4, 2), t, rest={"crossed": False}, end="filled")
                 + lifecycle((1, 5, 3), t, rest={"crossed": False, "expires_at": t + 100},
                             end="expired")
                 + lifecycle((1, 6, 4), t, end="filled")
                 + lifecycle((1, 3, 5), t, marketable=False, sealed=True, end="filled"))
        code, r = self.run_verdict(lines)
        self.assertEqual((code, r["pass"], r["slo"]), (0, True, 1.0))
        self.assertEqual((r["cleared"], r["missed"], r["stuck_open"]), (3, 0, []))
        self.assertEqual(r["breakdown"], {"resting": 1, "cleared": 3, "expired-nonmarketable": 1})
        self.assertTrue(r["sealed"]["zero_loss"])

    def test_a_leaked_resting_order_fails_and_a_rationed_expiry_does_not(self):
        t = time.time() - 5000
        lines = (lifecycle((1, 2, 6), t, marketable=False, rest={"crossed": True}, end="expired")
                 + lifecycle((1, 4, 7), t, rest={"crossed": False, "expires_at": t})
                 + lifecycle((1, 6, 8), t, end="filled"))
        code, r = self.run_verdict(lines)
        self.assertEqual((code, r["pass"]), (1, False))
        self.assertEqual((r["cleared"], r["missed"]), (1, 1), "only the leak counts")
        self.assertEqual([k for k, _ in r["stuck_open"]], [[1, 4, 7]], "past its own expiry")
        self.assertEqual(r["sample_missed"], [], "the rationed expiry is not a miss")


if __name__ == "__main__":
    unittest.main(verbosity=2)
