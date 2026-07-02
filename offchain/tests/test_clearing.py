"""Parity tests for the Python clearing port vs the Rust `match-engine`.

These mirror the scenarios in `crates/match-engine/src/lib.rs` so the builder's execution
report (which uses `clearing.clear`) is provably the SAME clearing `refine` performs. If
these ever diverge from the Rust results, the fill receipts would lie — so they're pinned.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from clearing import clear, BUY, SELL  # noqa: E402


def buy(oid, price, qty):
    return {"account": oid, "oid": oid, "side": BUY, "price": price, "qty": qty}


def sell(oid, price, qty):
    return {"account": oid, "oid": oid, "side": SELL, "price": price, "qty": qty}


class Clearing(unittest.TestCase):
    def test_no_cross_no_trade(self):
        c = clear([buy(1, 90, 10), sell(2, 100, 10)])
        self.assertEqual(c["volume"], 0)
        self.assertEqual(c["fills"], {})

    def test_uniform_price_buyer_pays_clearing_not_limit(self):
        # buyer bids 105 but clears at 100 (the price that maximizes volume)
        c = clear([buy(1, 105, 5), buy(2, 100, 5), sell(3, 100, 10)])
        self.assertEqual(c["price"], 100)
        self.assertEqual(c["volume"], 10)

    def test_buy_between_asks_clears_at_marginal_ask_only(self):
        # mirror of the Rust regression test: buy 100@125 vs asks 100@110/120/130 clears
        # 100 @ 110 (competitive equilibrium, zero imbalance); 120/130 don't trade.
        c = clear([sell(1, 110, 100), sell(2, 120, 100), sell(3, 130, 100), buy(4, 125, 100)])
        self.assertEqual(c["price"], 110)
        self.assertEqual(c["volume"], 100)
        self.assertEqual(c["fills"].get(1), 100)   # the 110 ask fills
        self.assertEqual(c["fills"].get(4), 100)   # the buyer fills
        self.assertNotIn(2, c["fills"])            # 120 ask untouched
        self.assertNotIn(3, c["fills"])            # 130 ask untouched

    def test_big_buy_partially_fills_uniform_price(self):
        # the user's scenario: BUY 500@125 vs asks 100@110/120/130.
        # Candidates: 110→v=100, 120→v=200, 125→v=200, 130→v=0. Max volume 200 at 120 & 125
        # (both imbalance 300); lowest-price tie-break → 120. So 200 clear at ONE price 120
        # (NOT 100@110 + 100@120), and 300 of the buy is unfilled.
        c = clear([sell(1, 110, 100), sell(2, 120, 100), sell(3, 130, 100), buy(4, 125, 500)])
        self.assertEqual(c["price"], 120, "uniform clearing price, not per-level prices")
        self.assertEqual(c["volume"], 200)
        self.assertEqual(c["fills"].get(4), 200, "buyer fills 200 of 500 → 300 unfilled")
        self.assertEqual(c["fills"].get(1), 100)   # 110 ask fully filled (at the 120 price)
        self.assertEqual(c["fills"].get(2), 100)   # 120 ask fully filled
        self.assertNotIn(3, c["fills"])            # 130 ask does not cross a 125 buy

    def test_marginal_rationing_is_price_time_deterministic(self):
        # demand 15 @≥100, supply 10 @100 → V=10; buys rationed by id: id1 (5) full,
        # id2 gets the remaining 5.
        c = clear([buy(1, 100, 5), buy(2, 100, 10), sell(3, 100, 10)])
        self.assertEqual(c["volume"], 10)
        self.assertEqual(c["fills"][1], 5)
        self.assertEqual(c["fills"][2], 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
