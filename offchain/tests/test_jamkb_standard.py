"""JAMKB-standard server behaviour — solvency backpressure + beneficiary top-up.

The pure rent/reserve/solvency logic lives in `treasury.py` (tested in `test_treasury.py`).
This file tests how `server.py` *wires* that standard: it refuses new state-growing orders
while under-reserved, exposes the backpressure flag, and lets the beneficiary top up the
reserve. Chain reads/writes are monkeypatched so no node is needed. Importing `server` is
safe (its startup is `__main__`-guarded). See docs/JAMKB_STANDARD.md.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

S = server.SCALE


class JamkbBackpressure(unittest.TestCase):
    def setUp(self):
        server.JAMKB_BACKPRESSURE = True
        self._sent = []
        server.submit = lambda payload: self._sent.append(payload)   # capture, don't hit a node
        # obligation = 8 KB (8192 octets); the reserve is held in JAMKB at FEE_ACCOUNT.
        server.footprint_octets = lambda: 8192

    def _hold(self, jamkb):
        # only JAMKB@FEE_ACCOUNT matters for solvency; everything else reads 0.
        server.bal = lambda asset, acct: (jamkb * S) if (asset == server.JAMKB and acct == server.FEE_ACCOUNT) else 0

    def test_under_reserved_blocks_new_orders(self):
        self._hold(3)   # hold 3 JAMKB vs an 8 KB obligation → insolvent
        solvent, short = server.jamkb_solvency()
        self.assertFalse(solvent)
        self.assertEqual(short, 5 * S)   # short 5 KB
        with self.assertRaises(ValueError):
            server.api_order({"market": 1, "side": "buy", "qty": 1, "price": 1,
                              "account": 0, "base": 0, "quote": 0})
        self.assertEqual(self._sent, [], "no state was written while under-reserved")

    def test_backpressure_flag_surfaces_in_status(self):
        self._hold(3)
        self.assertTrue(server.treasury_status()["backpressure"])

    def test_solvent_service_reports_no_backpressure(self):
        self._hold(8)   # exactly covers the 8 KB obligation
        solvent, short = server.jamkb_solvency()
        self.assertTrue(solvent)
        self.assertEqual(short, 0)
        self.assertFalse(server.treasury_status()["backpressure"])

    def test_disabling_backpressure_lets_the_gate_pass(self):
        # with JAMKB_BACKPRESSURE off the solvency gate is skipped entirely (the order then
        # fails later for other reasons, but NOT with the under-reserved message).
        server.JAMKB_BACKPRESSURE = False
        self._hold(0)
        try:
            server.api_order({"market": 1, "side": "buy", "qty": 1, "price": 1,
                              "account": 0, "base": 0, "quote": 0})
        except ValueError as e:
            self.assertNotIn("under-reserved", str(e))
        except Exception:
            pass   # any non-solvency failure is fine — the gate did not fire

    def test_topup_deposits_jamkb_to_the_reserve(self):
        self._hold(3)
        r = server.api_reserve_topup({"amount": 10})
        self.assertTrue(r["ok"])
        self.assertEqual(len(self._sent), 1, "top-up writes exactly one deposit")
        self.assertEqual(self._sent[0][0], server.TAG_DEPOSIT)

    def test_topup_rejects_nonpositive(self):
        with self.assertRaises(ValueError):
            server.api_reserve_topup({"amount": 0})

    def test_topup_is_capped_at_target_no_hoarding(self):
        # obligation 8 KB + buffer → a finite target; a top-up beyond it is refused, because
        # holding more JAMKB than you occupy is idle RAM rights denied to other services.
        self._hold(8)                       # already at the obligation
        target = server.reserve_target_atomic() / S
        with self.assertRaises(ValueError):
            server.api_reserve_topup({"amount": target + 100})   # over the target → rejected
        self.assertEqual(self._sent, [], "no unbounded mint")

    def test_status_reports_over_reserved_and_finite_supply(self):
        self._hold(1000)                    # holding 1000 JAMKB vs an 8 KB obligation → hoarding
        st = server.treasury_status()
        self.assertGreater(st["over_reserved_jamkb"], 0, "excess is flagged, not counted as profit")
        self.assertEqual(st["withdrawable"][server.JAMKB], 0, "JAMKB is never withdrawable profit")
        self.assertEqual(st["supply_jamkb"], server.JAMKB_SUPPLY)


if __name__ == "__main__":
    unittest.main(verbosity=2)
