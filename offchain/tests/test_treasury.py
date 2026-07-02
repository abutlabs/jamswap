"""Self-funding treasury tests — fees cover the JAMKB state rent first; only the
surplus is withdrawable profit.

Drives the pure `offchain/treasury.py` logic. No node needed. Run with:

    python3 -m unittest discover -s offchain/tests

Balances here are in whole tokens (the function is unit-agnostic; the server passes
atomic units = display x SCALE).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from treasury import jamkb_rent, profit_split, max_withdrawable, USDC, DOT, JAMKB  # noqa: E402


class Rent(unittest.TestCase):
    def test_ceil_division(self):
        self.assertEqual(jamkb_rent(0), 0)
        self.assertEqual(jamkb_rent(1), 1)         # 1 byte still needs 1 JAMKB
        self.assertEqual(jamkb_rent(1024), 1)
        self.assertEqual(jamkb_rent(1025), 2)      # rounds up
        self.assertEqual(jamkb_rent(4096), 4)


class ProfitSplit(unittest.TestCase):
    def test_under_reserved_no_profit_in_any_asset(self):
        # rent needs 10 JAMKB, treasury holds 4 -> shortfall 6, NOTHING withdrawable,
        # not even the USDC/DOT fees (rent must be covered first).
        t = {JAMKB: 4, USDC: 1000, DOT: 50}
        s = profit_split(t, rent_reserve=10)
        self.assertFalse(s["solvent"])
        self.assertEqual(s["shortfall"], 6)
        self.assertEqual(s["reserve_held"], 4)
        self.assertEqual(s["withdrawable"], {JAMKB: 0, USDC: 0, DOT: 0})

    def test_exactly_reserved_other_assets_are_profit(self):
        # holds exactly the rent in JAMKB -> JAMKB profit 0, but USDC/DOT fees withdrawable.
        t = {JAMKB: 10, USDC: 1000, DOT: 50}
        s = profit_split(t, rent_reserve=10)
        self.assertTrue(s["solvent"])
        self.assertEqual(s["shortfall"], 0)
        self.assertEqual(s["withdrawable"], {JAMKB: 0, USDC: 1000, DOT: 50})

    def test_over_reserved_excess_jamkb_is_profit(self):
        t = {JAMKB: 15, USDC: 1000, DOT: 50}
        s = profit_split(t, rent_reserve=10)
        self.assertEqual(s["reserve_held"], 10)
        self.assertEqual(s["withdrawable"], {JAMKB: 5, USDC: 1000, DOT: 50})

    def test_zero_rent_everything_is_profit(self):
        t = {JAMKB: 3, USDC: 100}
        s = profit_split(t, rent_reserve=0)
        self.assertTrue(s["solvent"])
        self.assertEqual(s["withdrawable"], {JAMKB: 3, USDC: 100})

    def test_does_not_mutate_input(self):
        t = {JAMKB: 15, USDC: 1000}
        before = dict(t)
        profit_split(t, 10)
        self.assertEqual(t, before)


class MaxWithdrawable(unittest.TestCase):
    def test_gate_matches_split(self):
        t = {JAMKB: 15, USDC: 1000, DOT: 50}
        self.assertEqual(max_withdrawable(t, 10, JAMKB), 5)
        self.assertEqual(max_withdrawable(t, 10, USDC), 1000)
        self.assertEqual(max_withdrawable(t, 10, DOT), 50)

    def test_under_reserved_gate_is_zero(self):
        t = {JAMKB: 4, USDC: 1000}
        self.assertEqual(max_withdrawable(t, 10, USDC), 0)
        self.assertEqual(max_withdrawable(t, 10, JAMKB), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
