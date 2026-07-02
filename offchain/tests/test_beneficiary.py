"""Beneficiary-sweep tests — the governance signature the server produces for a treasury
sweep must be accepted by the service's baked GOV_PUBKEY.

This verifies OFFLINE (no node) that a server-side beneficiary sweep will be honored
on-chain: `server.gov_sign` signs with the demo governance seed, and the service verifies
against the constant `GOV_PUBKEY` baked into `service/src/lib.rs`. If these agree, the
sweep transaction is valid by construction.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server  # noqa: E402

# the GOV_PUBKEY constant baked into service/src/lib.rs (derived from the demo seed)
GOV_PUBKEY_HEX = "374287634e129ec1f72c7508a130a6f4ae2c1456c9280fe52eaa4f2254f7e6ca"


class BeneficiarySweep(unittest.TestCase):
    def setUp(self):
        if not server.HAVE_NACL:
            self.skipTest("PyNaCl not installed (server-side gov signing needs it)")

    def test_gov_signature_verifies_against_baked_pubkey(self):
        from nacl.signing import VerifyKey
        msg = server.canon(b"treasury", b"\x00\x00\x00\x00", b"\x00" * 8, b"\x00\x00\x00\x00", b"\x00" * 8)
        sig = server.gov_sign(msg)
        # raises BadSignatureError if the server's key doesn't match the baked GOV_PUBKEY
        VerifyKey(bytes.fromhex(GOV_PUBKEY_HEX)).verify(msg, sig)

    def test_sweep_disabled_by_default(self):
        # safety: server-side gov signing is opt-in (it holds the demo gov key).
        self.assertFalse(server.BENEFICIARY_SWEEP,
                         "BENEFICIARY_SWEEP must default OFF (prototype security)")

    def test_sweep_rejected_when_disabled(self):
        if server.BENEFICIARY_SWEEP:
            self.skipTest("sweep enabled in this environment")
        with self.assertRaises(ValueError):
            server.api_beneficiary_sweep({"asset": 1, "dest": 5, "amount": 1})


if __name__ == "__main__":
    unittest.main(verbosity=2)
