"""Anti-bloat tests — no order rests forever; spam self-expires.

A resting order consumes JAMKB state rent continuously, so an unbounded good-till-cancelled
order is a griefing vector (spam the book with far orders that never fill or expire). The
defense: every order gets a rent-funded expiry (sealed sooner than public, hard-capped), and
a per-account open-order limit. These test the pure lifetime maths + that `api_order` always
stamps a bounded expiry and enforces the cap (chain I/O monkeypatched — no node).
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

S = server.SCALE


class Lifetime(unittest.TestCase):
    def test_sealed_expires_sooner_than_public(self):
        # a sealed commitment (32 B) has a bigger footprint than a public order (17 B), so its
        # rent budget runs out faster — sealed lives strictly shorter.
        self.assertLess(server.order_lifetime_secs(True), server.order_lifetime_secs(False))

    def test_lifetime_is_hard_capped(self):
        self.assertLessEqual(server.order_lifetime_secs(False), server.MAX_RESTING_SECS)
        self.assertLessEqual(server.order_lifetime_secs(True), server.MAX_RESTING_SECS)

    def test_lifetime_matches_budget_over_footprint(self):
        # public: budget / (17/1024)  — the documented rent formula
        expect = min(server.ORDER_RENT_BUDGET_KBS / (server.FOOTPRINT_PUBLIC / 1024.0),
                     server.MAX_RESTING_SECS)
        self.assertAlmostEqual(server.order_lifetime_secs(False), expect, places=3)


class ApiOrderExpiry(unittest.TestCase):
    def setUp(self):
        server.pending.clear()
        server.order_expiry.clear()
        server.JAMKB_BACKPRESSURE = False        # isolate: don't gate on solvency here
        server.REQUIRE_ORDER_SIG = False
        server.MAX_OPEN_ORDERS = 50
        server.footprint_octets = lambda: 0
        server.storage = lambda k: b""           # empty on-chain book
        server.submit = lambda payload: None
        server.mstate = lambda prefix, m: 0

    def _order(self, **kw):
        b = {"market": 1, "side": "buy", "qty": 1, "price": 1, "account": 7}
        b.update(kw)
        return server.api_order(b)

    def test_gtc_gets_a_bounded_expiry(self):
        # ttl 0 = "GTC" — but it must NOT be infinite; it expires when the rent budget runs out.
        r = self._order(ttl=0)
        exp = server.order_expiry[(1, 7, r["order_id"])]
        life = exp - time.time()
        self.assertGreater(life, 0)
        self.assertLessEqual(life, server.order_lifetime_secs(False) + 1)

    def test_user_ttl_can_shorten_but_not_extend(self):
        short = self._order(ttl=30)                                  # 30 s < rent lifetime → honored
        self.assertLessEqual(server.order_expiry[(1, 7, short["order_id"])] - time.time(), 31)
        huge = self._order(ttl=10**9)                               # absurd TTL is clamped to the cap
        self.assertLessEqual(server.order_expiry[(1, 7, huge["order_id"])] - time.time(),
                             server.order_lifetime_secs(False) + 1)

    def test_open_order_cap_rejects_spam(self):
        server.MAX_OPEN_ORDERS = 3
        for _ in range(3):
            self._order(ttl=0)
        with self.assertRaises(ValueError):
            self._order(ttl=0)                                       # 4th on the same market → refused


if __name__ == "__main__":
    unittest.main(verbosity=2)
