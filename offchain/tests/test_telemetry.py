"""The DEX's telemetry labels stay bounded (one series per refusal reason)."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("JAMSWAP_NO_SERVE", "1")
import server


class RefusalReason(unittest.TestCase):
    def test_numbers_masked_and_first_clause_kept(self):
        e = ValueError("open-order limit reached (16 per market) — cancel or let some clear before placing more")
        self.assertEqual(server.refusal_reason(e), "open-order limit reached (N per market)")

    def test_amounts_masked(self):
        a = server.refusal_reason(ValueError("insufficient USDC to fund this buy (need 1,234.5)"))
        b = server.refusal_reason(ValueError("insufficient USDC to fund this buy (need 7)"))
        self.assertEqual(a, b)

    def test_bounded_length(self):
        self.assertLessEqual(len(server.refusal_reason(ValueError("x" * 500))), 60)


if __name__ == "__main__":
    unittest.main()
