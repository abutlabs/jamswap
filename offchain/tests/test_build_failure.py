"""A failed chain read while building a round must not lose the batch (jamswap#4).

`_build_round` takes the round's orders out of the mempool, then reads the chain: the
market's last price (to price market orders) and each account's seq floor. A reader
timeout there used to escape with the batch in neither the mempool nor `_inflight`:
never submitted, and "open" in the order telemetry forever. Now every order the build
took is either ended with a terminal, registered in flight, or put back at the FRONT of
the mempool in the order it was taken — and the next auction submits it.

Drives `server.py` against the fake chain of test_round_poison.py. Run with:
    python3 -m unittest discover -s offchain/tests
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import server                                        # noqa: E402
import order_telemetry                               # noqa: E402
from test_round_poison import _Base, order, M, S, u32   # noqa: E402  (the fake-chain harness)


class ReadFailsMidBuild(_Base):
    def failing_storage(self, prefix, on_fail=None):
        """The reader times out for keys starting with `prefix` while switch["on"]."""
        real, switch = self.chain.get, {"on": True}

        def storage(key):
            if switch["on"] and bytes(key).startswith(prefix):
                if on_fail:
                    on_fail()
                raise TimeoutError("reader timed out")
            return real(key)
        server.storage = storage
        return switch

    def assert_back_and_untouched(self, oids):
        self.assertEqual(self.pending_oids(), oids, "every order back, in mempool order")
        self.assertNotIn(M, server._inflight, "no round registered")
        self.assertEqual(self.sent, [], "nothing submitted")
        lk = server._market_lock(M)
        self.assertTrue(lk.acquire(blocking=False), "the market lock was released")
        lk.release()

    def test_a_seq_floor_read_timeout_puts_the_batch_back_and_the_next_auction_submits_it(self):
        self.queue(order(7, 1, "buy", 1, 10, seq=15), order(8, 2, "sell", 1, 10, seq=3),
                   order(9, 3, "buy", 1, 5, seq=1))
        arrived = []

        def arrive():                                # api_order lands while the build reads
            if not arrived:
                arrived.append(order(6, 4, "sell", 1, 5, seq=1))
                self.queue(arrived[0])
        switch = self.failing_storage(b"sq", on_fail=arrive)
        with self.assertRaises(TimeoutError):
            self.build()
        self.assert_back_and_untouched([1, 2, 3, 4])   # the batch ahead of the new arrival
        for oid, acct in ((1, 7), (2, 8), (3, 9)):
            self.assertTrue(order_telemetry.is_open(M, acct, oid))
            self.assertEqual(order_telemetry._orders[(M, acct, oid)]["phase"], "placed")
        switch["on"] = False                         # the reader recovers: no cooldown owed
        self.build()
        self.assertEqual(len(self.sent), 1, "the next auction submits them")
        self.assertEqual(sorted(o["oid"] for o in server._inflight[M]["public"]), [1, 2, 3, 4])
        self.assertEqual(self.pending_oids(), [])

    def test_a_last_price_read_timeout_puts_the_batch_back(self):
        self.lp = S
        self.queue(order(7, 1, "buy", 0, 10, seq=15, otype="market"),
                   order(8, 2, "sell", 1, 10, seq=3))
        mstate, fail = server.mstate, {"on": True}

        def flaky_mstate(prefix, m):
            if fail["on"] and prefix == b"lp":
                raise TimeoutError("reader timed out")
            return mstate(prefix, m)
        server.mstate = flaky_mstate
        with self.assertRaises(TimeoutError):
            self.build()
        self.assert_back_and_untouched([1, 2])
        fail["on"] = False
        self.build()
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(sorted(o["oid"] for o in server._inflight[M]["public"]), [1, 2])

    def test_orders_the_build_already_ended_are_not_requeued(self):
        # acct 7's order is superseded (its floor passed it): the build ends it "rejected".
        # A failure AFTER that must re-queue only the live pair — a re-queued dead order
        # would be rejected a second time (two terminals for one order).
        self.chain.set_floor(b"sq", 7, 20)
        self.queue(order(7, 1, "buy", 1, 5, seq=15), order(8, 2, "sell", 1, 10, seq=3),
                   order(9, 3, "buy", 1, 10, seq=1))
        clear = server.clear
        self.addCleanup(setattr, server, "clear", clear)

        def broken(orders):
            raise RuntimeError("clearing blew up")
        server.clear = broken
        with self.assertRaises(RuntimeError):
            self.build()
        self.assert_back_and_untouched([2, 3])
        self.assertEqual(self.dispositions(7), ["rejected"])
        self.assertFalse(order_telemetry.is_open(M, 7, 1))
        server.clear = clear
        self.build()
        self.assertEqual(sorted(o["oid"] for o in server._inflight[M]["public"]), [2, 3])
        self.assertEqual(self.dispositions(7), ["rejected"], "ended once, not twice")


class CommitteeFailure(_Base):
    def test_a_failed_committee_run_requeues_once_and_cools_down(self):
        # the one pre-existing restore path (encrypt-until-batch): it now shares the
        # generic one, which must not queue the order twice
        server.ENC_MODE = True

        def committee(cmd, *args):
            if cmd == "encrypt":
                return {"ciphertext": (b"\x02" * 32 + bytes.fromhex(args[1])).hex()}
            raise RuntimeError("committee sidecar failed")
        server.committee_run = committee
        self.chain.set_book(server.order_bytes(20, 1, server.SELL, S, 10 * S))
        o = self.sealed_buy(7, 50, 10)
        self.chain.kv[b"encset" + u32(M)] = server._consumed_entry(o)
        self.queue(o, order(8, 2, "sell", 1, 1, seq=3))
        with self.assertRaises(RuntimeError):
            self.build()
        self.assertEqual(self.pending_oids(), [50, 2])
        self.assertNotIn(M, server._inflight)
        self.assertIn(M, server._round_gate, "the market cools down after a committee failure")


if __name__ == "__main__":
    unittest.main(verbosity=2)
