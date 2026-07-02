"""Round-lifecycle tests — the sealed-order matching behaviour, across sequences of
auctions.

These are the regression tests for the bug where a sealed sell placed in one 6 s
window and a sealed buy placed in a later window never met (sealed orders were
immediate-or-cancel and drained every tick). They drive the pure `plan_round` planner
(`offchain/round.py`) through multi-round scenarios and assert *which orders clear,
which rest hidden, and which expire* — the matching behaviour vs expected.

No node, no committee binary, no docker: pure logic, fast, deterministic. Run with:

    python3 -m unittest discover -s offchain/tests
    # or:  python3 offchain/tests/test_round_lifecycle.py

Prices here are plain integers (the planner is scale-agnostic — it only compares
prices, so $1 vs $2 is modelled as 1 vs 2).
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from round import plan_round, BUY, SELL  # noqa: E402

_oid = [0]


def order(side, price, qty=10, sealed=False, expiry=None, account=1):
    _oid[0] += 1
    return {"account": account, "oid": _oid[0], "side": side, "price": price,
            "qty": qty, "sealed": sealed, "expiry": expiry}


# sealed helpers (the default in these tests — most scenarios are about sealed orders)
def buy(price, **kw):  return order(BUY, price, sealed=True, **kw)
def sell(price, **kw): return order(SELL, price, sealed=True, **kw)
# explicit public (plaintext) helpers
def pbuy(price, **kw):  return order(BUY, price, sealed=False, **kw)
def psell(price, **kw): return order(SELL, price, sealed=False, **kw)


def run_round(pending, resting=None, now=0.0):
    """Run one round; return (plan, next_pending, next_resting).

    Models the server's carry-forward: sealed orders that don't cross stay pending for
    the next round; revealed sealed + public orders leave the pending queue. (Public
    orders that don't fill would rest in the on-chain book; these planner tests focus
    on the sealed lifecycle, so `resting` is supplied explicitly per round.)"""
    resting = resting or []
    plan = plan_round(pending, resting, now)
    return plan, list(plan.carry), resting


class SealedCrossBatch(unittest.TestCase):
    """The reported bug: sealed sells then, later, sealed buys — must eventually match."""

    def test_lone_sealed_sells_rest_hidden_then_buys_cross(self):
        # Round 1: four sealed sells @1, nothing to trade with -> all rest HIDDEN
        # (this is the fix; the old behaviour discarded them as immediate-or-cancel).
        sells = [sell(1, qty=10) for _ in range(4)]
        plan, carried, _ = run_round(sells)
        self.assertEqual(len(plan.reveal), 0, "no counterparty yet -> reveal nothing")
        self.assertEqual(len(plan.carry), 4, "all four sealed sells rest hidden")
        self.assertEqual(len(plan.expired), 0)

        # Round 2: two sealed buys @1 arrive. Now buys cross the carried sells ->
        # every crossing order is revealed together and clears in ONE auction.
        buys = [buy(1, qty=10) for _ in range(2)]
        plan2, carried2, _ = run_round(carried + buys)
        revealed_sides = sorted(o["side"] for o in plan2.reveal)
        self.assertEqual(len(plan2.reveal), 6, "4 sells + 2 buys all cross at price 1")
        self.assertEqual(revealed_sides, [BUY, BUY, SELL, SELL, SELL, SELL])
        self.assertEqual(len(plan2.carry), 0, "everything crossed -> nothing left hidden")

    def test_buys_first_then_sells(self):
        # Symmetric: sealed buys rest, then a sealed sell crosses them.
        plan, carried, _ = run_round([buy(5), buy(5)])
        self.assertEqual(len(plan.carry), 2)
        plan2, _, _ = run_round(carried + [sell(5)])
        self.assertEqual(len(plan2.reveal), 3, "the sell crosses both resting buys")


class SameBatchMatching(unittest.TestCase):
    def test_sealed_buy_and_sell_same_round_cross(self):
        plan, _, _ = run_round([buy(1), sell(1)])
        self.assertEqual(len(plan.reveal), 2, "same-batch crossing sealed orders clear together")
        self.assertEqual(len(plan.carry), 0)

    def test_non_crossing_prices_both_rest(self):
        # buy @1 vs sell @2 — the spread doesn't cross, so neither trades this round;
        # both rest hidden (a marketable counterparty may arrive later).
        plan, _, _ = run_round([buy(1), sell(2)])
        self.assertEqual(len(plan.reveal), 0)
        self.assertEqual(len(plan.carry), 2)

    def test_sealed_crosses_resting_public_book(self):
        # A resting PUBLIC sell @1 is in the on-chain book; a new sealed buy @1 crosses
        # it and is revealed to trade against it.
        resting = [{"side": SELL, "price": 1, "qty": 10}]
        plan, _, _ = run_round([buy(1)], resting=resting)
        self.assertEqual(len(plan.reveal), 1)

    def test_sealed_below_resting_book_rests_hidden(self):
        resting = [{"side": SELL, "price": 5, "qty": 10}]
        plan, _, _ = run_round([buy(1)], resting=resting)
        self.assertEqual(len(plan.reveal), 0)
        self.assertEqual(len(plan.carry), 1)

    def test_all_crossing_orders_revealed_even_if_rationed_out(self):
        # CORRECTNESS INVARIANT: every sealed order that CROSSES is revealed, even if it
        # would be rationed to a zero fill this round. Carrying a marketable order forward
        # (instead of revealing it) could change the uniform clearing price for the others,
        # so only STRICTLY non-crossing orders may be carried. Here two buys @1 and a sell
        # of 10 @1: both buys cross (one may not fully fill), so BOTH are revealed — none
        # carried. A future "carry the loser" optimisation must not break this.
        plan, _, _ = run_round([buy(1, qty=10), buy(1, qty=10), sell(1, qty=10)])
        self.assertEqual(len(plan.reveal), 3, "all crossing orders revealed together")
        self.assertEqual(len(plan.carry), 0, "no crossing order is ever carried forward")


class Expiry(unittest.TestCase):
    def test_gtt_sealed_order_expires_if_never_crossed(self):
        # good-till-time in the past, no counterparty -> dropped, not carried.
        plan, _, _ = run_round([sell(1, expiry=100.0)], now=200.0)
        self.assertEqual(len(plan.carry), 0)
        self.assertEqual(len(plan.expired), 1)

    def test_gtt_sealed_order_carries_before_expiry(self):
        plan, _, _ = run_round([sell(1, expiry=300.0)], now=200.0)
        self.assertEqual(len(plan.carry), 1)
        self.assertEqual(len(plan.expired), 0)

    def test_crossing_order_reveals_even_if_expiring(self):
        # if it crosses, it trades this round regardless of expiry (expiry only drops
        # orders that had no chance to trade).
        plan, _, _ = run_round([buy(1), sell(1, expiry=1.0)], now=999.0)
        self.assertEqual(len(plan.reveal), 2)
        self.assertEqual(len(plan.expired), 0)


class PublicOrdersAreOneShot(unittest.TestCase):
    def test_public_orders_never_carry(self):
        # public (unsealed) orders always go into this round; the carry logic is for
        # sealed orders only. (Unfilled public orders rest in the on-chain book, which
        # the service handles — not the sealed carry path.)
        plan, _, _ = run_round([pbuy(1), psell(9)])
        self.assertEqual(len(plan.public), 2)
        self.assertEqual(len(plan.carry), 0)
        self.assertEqual(len(plan.reveal), 0)


class Purity(unittest.TestCase):
    def test_plan_round_does_not_mutate_inputs(self):
        pending = [buy(1), sell(9)]
        before = [dict(o) for o in pending]
        plan_round(pending, [], now=0.0)
        self.assertEqual(pending, before, "planner must not mutate its inputs")

    def test_deterministic(self):
        pending = [buy(1), sell(1), buy(2)]
        a = plan_round(pending, [], 0.0)
        b = plan_round(pending, [], 0.0)
        self.assertEqual([o["oid"] for o in a.reveal], [o["oid"] for o in b.reveal])
        self.assertEqual([o["oid"] for o in a.carry], [o["oid"] for o in b.carry])


if __name__ == "__main__":
    unittest.main(verbosity=2)
