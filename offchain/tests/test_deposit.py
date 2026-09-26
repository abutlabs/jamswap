"""Deposit producers carry an idempotency nonce (jamswap#7).

The service credits each (account, nonce) once (`crates/match-engine/src/deposit.rs`, where
the rule is host-tested: duplicate / replay / reordering). This file pins the producer side:
every DEPOSIT the builder sends is the 25-byte layout the service decodes, byte for byte, and
carries a nonce that is fresh per deposit (so two real deposits are both credited) or, when the
caller supplies one, stable across retries (so a retried request is credited once). Chain I/O
is stubbed; no node is needed.
"""
import os
import struct
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

S = server.SCALE

# crates/match-engine/src/deposit.rs `fixture_decodes_as_the_server_encodes_it` decodes these
# exact bytes as account 7, asset 1, amount 50_000, nonce 0x0102030405060708
RUST_FIXTURE = "01070000000100000050c30000000000000807060504030201"


def fields(payload):
    """[tag][account][asset][amount][nonce] → dict (fails on any other length)."""
    tag, account, asset, amount, nonce = struct.unpack("<BIIQQ", payload)
    return {"tag": tag, "account": account, "asset": asset, "amount": amount, "nonce": nonce}


class DepositPayload(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.patches = [
            mock.patch.object(server, "submit", lambda payload, check=None, detail="": self.sent.append(payload)),
            mock.patch.object(server, "bal", lambda asset, acct: 0),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()

    def test_payload_matches_the_service_fixture(self):
        self.assertEqual(server.deposit_payload(7, 1, 50_000, nonce=0x0102030405060708).hex(), RUST_FIXTURE)

    def test_each_deposit_gets_a_fresh_increasing_nonce(self):
        # two real deposits to one account (the signed-ops e2e funds DOT then USDC back to
        # back): both must be credited, so their nonces differ
        server.api_deposit({"account": 7, "asset": server.DOT, "amount": 5})
        server.api_deposit({"account": 7, "asset": server.USDC, "amount": 5})
        a, b = map(fields, self.sent)
        self.assertEqual((a["tag"], a["account"], a["asset"], a["amount"]), (server.TAG_DEPOSIT, 7, server.DOT, 5 * S))
        self.assertEqual((b["asset"], b["amount"]), (server.USDC, 5 * S))
        self.assertGreater(a["nonce"], 0)
        self.assertGreater(b["nonce"], a["nonce"])

    def test_nonces_stay_increasing_when_the_clock_steps_back(self):
        with mock.patch.object(server, "_deposit_nonce_last", [0]), \
                mock.patch.object(server.time, "time_ns", side_effect=[5_000, 4_000, 4_000, 9_000]):
            ns = [server.deposit_nonce() for _ in range(4)]
        self.assertEqual(ns, [5_000, 5_001, 5_002, 9_000], "never reused, and back on the clock after")

    def test_a_callers_nonce_makes_a_retried_request_the_same_deposit(self):
        body = {"account": 7, "asset": server.DOT, "amount": 5, "nonce": 42}
        server.api_deposit(body)
        server.api_deposit(dict(body))              # the client retried after a timeout
        self.assertEqual(self.sent[0], self.sent[1], "byte-identical: the service credits it once")
        self.assertEqual(fields(self.sent[0])["nonce"], 42)

    def test_an_out_of_range_nonce_is_refused_before_anything_is_sent(self):
        for bad in (0, -1, 2 ** 64):
            with self.assertRaises(ValueError):
                server.api_deposit({"account": 7, "asset": server.DOT, "amount": 5, "nonce": bad})
        self.assertEqual(self.sent, [])

    def test_treasury_reserve_deposits_carry_a_nonce_too(self):
        # the reserve top-up and the startup seeding are DEPOSITs to the fee account
        with mock.patch.object(server, "reserve_target_atomic", lambda: 10 * S):
            server.api_reserve_topup({"amount": 3})
            server.ensure_reserve()
        topup, seed = map(fields, self.sent)
        self.assertEqual((topup["account"], topup["asset"], topup["amount"]), (server.FEE_ACCOUNT, server.JAMKB, 3 * S))
        self.assertEqual((seed["account"], seed["amount"]), (server.FEE_ACCOUNT, 10 * S))
        self.assertGreater(seed["nonce"], topup["nonce"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
