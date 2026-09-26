"""The DEX's starting state made on chain (offchain/dex_setup.py): markets listed, the dev
accounts registered in order and funded once, against fake_jam.FakeJamNode (a fake chain
whose jamswap-like service applies LIST / REGISTER / DEPOSIT with the service's keys and
idempotency rules).

Run with:  python3 -m unittest discover -s offchain/tests
"""
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chain                                                    # noqa: E402
import dex_setup                                                # noqa: E402
import workpackage as wp                                        # noqa: E402
from fake_jam import AUTH_CODE, Account, FakeClock, FakeJamNode, H   # noqa: E402

DEX_CODE = b"\x00jamswap-like"
SID = 5
PUBS = ["4418fb8c85bb3985394a8c2756d3643457ce614546202a2f50b093d762499ace",   # docs.jamcha.in
        "ad93247bd01307550ec7acd757ce6fb805fcf73db364063265b30a949e90d933",
        "cab2b9ff25c2410fbe9b8a717abb298c716a03983c98ceb4def2087500b8e341",
        "f30aa5444688b3cab47697b37d5cac5707bb3289e986b19b17db437206931a8d",
        "8b8c5d436f92ecf605421e873a99ec528761eb52a88a2f9a057b3b3003e6f32a",
        "ab0084d01534b31c1dd87c81645fd762482a90027754041ca1b56133d0466c06"]


try:
    import nacl.signing                                         # noqa: F401
    HAVE_NACL = True
except ImportError:
    HAVE_NACL = False
needs_nacl = unittest.skipUnless(HAVE_NACL, "PyNaCl not installed (the dev accounts sign)")


def u32(x):
    return struct.pack("<I", x)


class Payloads(unittest.TestCase):
    @needs_nacl
    def test_dev_accounts_are_the_standard_ones(self):
        self.assertEqual([bytes(sk.verify_key).hex() for _, sk in dex_setup.dev_keys()], PUBS)

    def test_layouts(self):
        self.assertEqual(dex_setup.list_payload(1, 1, 0), bytes([6]) + u32(1) + u32(1) + u32(0))
        # the shared fixture of deposit.rs and tests/test_deposit.py
        self.assertEqual(dex_setup.deposit_payload(7, 1, 50_000, 0x0102030405060708).hex(),
                         "01070000000100000050c30000000000000807060504030201")
        with self.assertRaises(ValueError):
            dex_setup.deposit_payload(7, 1, 1, 0)

    @needs_nacl
    def test_register(self):
        name, sk = dex_setup.dev_keys()[0]
        p = dex_setup.register_payload(sk)
        self.assertEqual((name, len(p), p[0], p[1:33].hex()), ("Alice", 97, 7, PUBS[0]))
        sk.verify_key.verify(b"jamswap:v1:register" + p[1:33], p[33:])

    def test_wanted(self):
        self.assertTrue(dex_setup.wanted({}, deployed=True))
        self.assertFalse(dex_setup.wanted({}, deployed=False))
        self.assertTrue(dex_setup.wanted({"DEX_SETUP": "1"}, deployed=False))
        self.assertFalse(dex_setup.wanted({"DEX_SETUP": "0"}, deployed=True))
        self.assertTrue(dex_setup.wanted({"DEX_SETUP": "auto"}, deployed=True))


class KvChain(chain.Chain):
    """read() over a dict, for the landed predicates."""
    def __init__(self, kv):
        super().__init__(1)
        self.kv = kv

    def read(self, key, at="best"):
        return self.kv.get(key, b"")


class Landed(unittest.TestCase):
    def test_deposit_record(self):
        kv = {b"dn" + u32(3): struct.pack("<3Q", 10, 12, 40)}   # floor 10, window {12, 40}
        c = KvChain(kv)
        self.assertEqual([dex_setup.deposit_landed(c, 3, n) for n in (1, 10, 11, 12, 40, 41)],
                         [True, True, False, True, True, False])
        self.assertFalse(dex_setup.deposit_landed(c, 4, 1))

    def test_handle_and_market(self):
        pk = bytes(32)
        c = KvChain({b"h" + pk: u32(9), b"mkt" + u32(2): u32(2) + u32(0)})
        self.assertEqual(dex_setup.handle_of(c, pk), 9)
        self.assertIsNone(dex_setup.handle_of(c, b"\x01" * 32))
        self.assertTrue(dex_setup.market_listed(c, 2))
        self.assertFalse(dex_setup.market_listed(c, 1))


@needs_nacl
class Setup(unittest.TestCase):
    def setUp(self):
        self.node = FakeJamNode(dex_code_hashes=[H(DEX_CODE)])
        self.node.accounts[SID] = Account(H(DEX_CODE), 0, 0)
        self.node.advance()
        self.t = FakeClock(self.node)
        self.ch = chain.Jip2Chain(SID, self.node.url, timeout=5,
                                  authorizer=wp.Authorizer(0, H(AUTH_CODE)))
        self.logs = []

    def tearDown(self):
        self.ch.rpc.close()
        self.node.stop()

    def setup_dex(self, **kw):
        runner = dex_setup.Runner(self.ch, self.logs.append, poll=6, resend=30,
                                  sleep=self.t.sleep, clock=self.t.clock)
        return dex_setup.setup(self.ch, runner=runner, **kw)

    def payloads(self):
        return [[w.payload for w in pkg.items] for _, pkg in self.node.packages_for(SID)]

    def test_a_fresh_service(self):
        handles = self.setup_dex(balance=100)
        self.assertEqual(handles, {"Alice": 1, "Bob": 2, "Carol": 3, "David": 4, "Eve": 5, "Fergie": 6})
        first, *deposits = self.payloads()
        # markets, then the registrations in account order: one package
        self.assertEqual([p[0] for p in first], [6, 6, 6] + [7] * 6)
        self.assertEqual([p[1:33].hex() for p in first[3:]], PUBS)
        # 18 deposits in packages of at most I = 16 items, nonce = asset + 1
        self.assertEqual([len(p) for p in deposits], [16, 2])
        st = self.node.storage(SID)
        for h in range(1, 7):
            for a in dex_setup.ASSETS:
                self.assertEqual(st[b"b" + u32(a) + u32(h)], struct.pack("<Q", 100 * dex_setup.SCALE))
            self.assertEqual(st[b"dn" + u32(h)], struct.pack("<4Q", 0, 1, 2, 3))
        self.assertEqual(st[b"markets"], u32(1) + u32(2) + u32(3))
        self.assertEqual(st[b"mkt" + u32(3)], u32(dex_setup.JAMKB) + u32(dex_setup.DOT))

    def test_a_rerun_submits_nothing(self):
        self.setup_dex(balance=100)
        n = len(self.node.packages)
        self.assertEqual(self.setup_dex(balance=100)["Fergie"], 6)
        self.assertEqual(len(self.node.packages), n)

    def test_only_what_is_missing(self):
        # Carol registered herself earlier (handle 1) and has had her USDC bootstrap deposit
        # (nonce 1); market 2 is listed
        st = self.node.accounts[SID].storage
        carol = bytes.fromhex(PUBS[2])
        st.update({b"h" + carol: u32(1), b"pk" + u32(1): carol, b"nexthandle": u32(2),
                   b"dn" + u32(1): struct.pack("<2Q", 0, 1),
                   b"mkt" + u32(2): u32(2) + u32(0), b"markets": u32(2)})
        self.node.advance()
        handles = self.setup_dex(balance=1)
        first, *deposits = self.payloads()
        self.assertEqual([p[0] for p in first], [6, 6] + [7] * 5)
        self.assertEqual([p[1] for p in first[:2]], [1, 3])
        self.assertEqual((handles["Carol"], handles["Alice"], handles["Fergie"]), (1, 2, 6))
        sent = [struct.unpack_from("<II", p, 1) for d in deposits for p in d]
        self.assertEqual(len(sent), 17)
        self.assertNotIn((1, dex_setup.USDC), sent)
        self.assertNotIn(b"b" + u32(dex_setup.USDC) + u32(1), st)

    def test_a_lost_package_is_resent(self):
        self.node.drop = 1                      # the first package is Failed
        self.setup_dex(balance=1)
        first, second = self.payloads()[:2]
        self.assertEqual(first, second, "the same ops, resent")
        self.assertTrue(any("failed" in m for m in self.logs), self.logs)

    def test_silence_is_resent_after_a_while(self):
        self.node.delay = 8                     # lands, but only after `resend` (30 s = 5 blocks)
        self.setup_dex(balance=0)
        self.assertEqual(len(self.payloads()), 2)
        self.assertEqual(len(self.node.storage(SID)[b"markets"]), 12, "LIST is idempotent")

    def test_times_out(self):
        self.node.drop = 1000
        runner = dex_setup.Runner(self.ch, self.logs.append, poll=6, resend=30,
                                  sleep=self.t.sleep, clock=self.t.clock)
        with self.assertRaisesRegex(dex_setup.SetupError, "not landed"):
            dex_setup.setup(self.ch, runner=runner, timeout=120)


class OnePayloadAtATime(unittest.TestCase):
    """A backend without multi-item packages (jamnp): one submit per op."""
    def test_sequential(self):
        kv, sent = {}, []

        class Sequential(KvChain):
            def submit(self, payload):
                sent.append(payload)
                if payload[0] == 6:
                    kv[b"mkt" + payload[1:5]] = payload[5:13]
                return {"accepted": True}
        t = [0.0]
        runner = dex_setup.Runner(Sequential(kv), lambda m: None, poll=1, resend=10,
                                  sleep=lambda s: t.__setitem__(0, t[0] + s), clock=lambda: t[0])
        runner.run("markets", [dex_setup.Op(f"m{m}", dex_setup.list_payload(m, 1, 0),
                                            lambda m=m: dex_setup.market_listed(runner.chain, m))
                               for m in (1, 2)])
        self.assertEqual([p[1] for p in sent], [1, 2])


class ServerStartup(unittest.TestCase):
    """server.ensure_markets lists only the markets not on chain yet."""
    def test_ensure_markets(self):
        import server
        listed = []
        saved = server.CHAIN, server.api_list
        server.CHAIN = KvChain({b"mkt" + u32(2): u32(2) + u32(0)})
        server.api_list = listed.append
        try:
            server.ensure_markets()
        finally:
            server.CHAIN, server.api_list = saved
        self.assertEqual([b["market"] for b in listed], [1, 3])
        self.assertIs(server.DEFAULT_MARKETS, dex_setup.DEFAULT_MARKETS)


class ReserveBeforeTrading(unittest.TestCase):
    """server.wait_reserved: the API opens once the JAMKB reserve covers the footprint."""
    def run_wait(self, solvency, timeout=10):
        import server
        t, sleeps = [0.0], []

        def sleep(s):
            sleeps.append(s)
            t[0] += s
        saved = server.jamkb_solvency, server.JAMKB_BACKPRESSURE
        server.jamkb_solvency, server.JAMKB_BACKPRESSURE = solvency, True
        try:
            return server.wait_reserved(timeout, poll=2, sleep=sleep, clock=lambda: t[0]), sleeps
        finally:
            server.jamkb_solvency, server.JAMKB_BACKPRESSURE = saved

    def test_waits_for_the_deposit_to_land(self):
        answers = iter([(False, 5), (False, 5), (True, 0)])
        ok, sleeps = self.run_wait(lambda: next(answers))
        self.assertEqual((ok, len(sleeps)), (True, 2))

    def test_gives_up_after_the_timeout(self):
        ok, sleeps = self.run_wait(lambda: (False, 5), timeout=10)
        self.assertEqual((ok, sum(sleeps)), (False, 10))

    def test_an_unreadable_footprint_is_waited_out_too(self):
        def boom():
            raise TimeoutError("reader")
        self.assertFalse(self.run_wait(boom, timeout=4)[0])


if __name__ == "__main__":
    unittest.main()
