"""The jip2 backend's submission (chain.Jip2Chain.submit): a GP 0.8.0 work-package built
from the chain and sent with JIP-2 submitWorkPackage, against fake_jip2.FakeJip2Node.

Each test decodes what the fake node received and checks it field by field against what
the node served: the refine context (the best block's parent as anchor, with its state
and BEEFY roots; the finalized block's parent as lookup anchor), the service's code
hash, the gas limits from the chain parameters, the authorizer, the core. The live check
against a stock PolkaJam node is manual (see the submission commit message).

Run with:  python3 -m unittest discover -s offchain/tests
"""
import base64
import hashlib
import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chain                                    # noqa: E402
import workpackage as wp                        # noqa: E402
from fake_jip2 import FakeJip2Node, RpcError    # noqa: E402
from test_workpackage import spec_with          # noqa: E402

B64 = lambda b: base64.b64encode(b).decode()    # noqa: E731
UNB64 = base64.b64decode
H = lambda b: hashlib.blake2b(b, digest_size=32).digest()   # noqa: E731

SID = 42
BEST, PARENT, GRANDPARENT = b"\xbb" * 32, b"\xaa" * 32, b"\x99" * 32
OLD, OLD_PARENT = b"\x77" * 32, b"\x76" * 32               # a finalized block far behind
SLOTS = {BEST: 1000, PARENT: 999, GRANDPARENT: 998, OLD: 970, OLD_PARENT: 969}
ROOTS = {BEST: b"\x01" * 32, PARENT: b"\x02" * 32, GRANDPARENT: b"\x03" * 32,
         OLD: b"\x04" * 32, OLD_PARENT: b"\x05" * 32}
PARENTS = {BEST: PARENT, PARENT: GRANDPARENT, OLD: OLD_PARENT}
L, RECENT = 24, 8                                           # max_lookup_anchor_age, H
BEEFY = {BEST: b"\x11" * 32, PARENT: b"\x12" * 32}
CODE_HASH = b"\xcc" * 32
AUTH_CODE = b"the authorizer's code"
AUTH = wp.Authorizer(0, H(AUTH_CODE))
G_R, G_A = 5_000_000_000, 10_000_000


def record(code_hash=CODE_HASH, min_item_gas=10_000):
    return struct.pack("<B32s5Q4I", 0, code_hash, 10 ** 12, min_item_gas, 10_000, 4242, 0,
                       17, 100, 200, 0)


class FakeSubmitNode(FakeJip2Node):
    """Three blocks and one service; records every package it is sent. `refuse` maps a
    core to the JSON-RPC error submitWorkPackage answers for it."""
    def __init__(self):
        self.sent = []                       # (core, package bytes, extrinsics)
        self.refuse = {}
        self.min_item_gas = 10_000
        super().__init__({
            "bestBlock": lambda: {"header_hash": B64(BEST), "slot": SLOTS[BEST]},
            "finalizedBlock": lambda: {"header_hash": B64(PARENT), "slot": SLOTS[PARENT]},
            "parent": self._parent,
            "stateRoot": lambda hh: B64(self._known(hh, ROOTS)),
            "beefyRoot": lambda hh: B64(self._known(hh, BEEFY)),
            "serviceData": self._service_data,
            "servicePreimage": self._preimage,
            "parameters": lambda: {"V1": {"core_count": 2, "max_refine_gas": G_R,
                                          "max_accumulate_gas": G_A,
                                          "max_lookup_anchor_age": L,
                                          "recent_block_count": RECENT}},
            "submitWorkPackage": self._submit,
            "workPackageStatus": lambda hh, ph, a: {"Reportable": {"remaining_blocks": 8}},
        })

    @staticmethod
    def _known(hh, table):
        v = table.get(UNB64(hh))
        if v is None:
            raise RpcError(1, "Block unavailable", hh)
        return v

    def _service_data(self, hh, s):
        return B64(record(min_item_gas=self.min_item_gas)) if s == SID else None

    @staticmethod
    def _preimage(hh, s, ph):
        return B64(AUTH_CODE) if (s, UNB64(ph)) == (AUTH.host, AUTH.code_hash) else None

    def _parent(self, hh):
        h = UNB64(hh)
        p = PARENTS.get(h)
        if p is None:
            raise RpcError(1, "Block unavailable", hh)
        return {"header_hash": B64(p), "slot": SLOTS[p]}

    def _submit(self, core, package, extrinsics):
        if core in self.refuse:
            raise self.refuse[core]
        self.sent.append((core, UNB64(package), extrinsics))
        return None

    def calls(self, method):
        return [r for r in self.requests if r["method"] == method]


class Submission(unittest.TestCase):
    def setUp(self):
        self.node = FakeSubmitNode()
        self.c = chain.Jip2Chain(SID, self.node.url, timeout=5, authorizer=AUTH)

    def tearDown(self):
        self.c.rpc.close()
        self.node.stop()

    def test_one_spec_valid_package_per_payload(self):
        self.assertTrue(self.c.submits)
        receipt = self.c.submit(b"\x07register-me")
        (core, package, extrinsics), = self.node.sent
        self.assertEqual((core, extrinsics), (0, []))
        p = wp.WorkPackage.decode(package)
        # the authorizer: host, code hash, empty token and configuration
        self.assertEqual((p.auth_code_host, p.auth_code_hash, p.authorization, p.authorizer_config),
                         (0, H(AUTH_CODE), b"", b""))
        # the context: the best block's parent as anchor, with its roots; the finalized
        # block's parent as lookup anchor, with its state root
        self.assertEqual(p.context, wp.RefineContext(
            PARENT, 999, ROOTS[PARENT], BEEFY[PARENT], GRANDPARENT, 998, ROOTS[GRANDPARENT], ()))
        # one item: the service's code hash as the chain holds it, the largest gas limits
        self.assertEqual(p.items, (wp.WorkItem(SID, CODE_HASH, b"\x07register-me", G_R - 1, G_A - 1),))
        self.assertEqual(receipt, {
            "accepted": True, "package_hash": H(package).hex(), "core": 0,
            "anchor": PARENT.hex(), "anchor_slot": 999, "lookup_anchor": GRANDPARENT.hex(),
            "lookup_anchor_slot": 998,
            "refused": [], "via": self.node.url[len("ws://"):]})

    def test_consecutive_packages_alternate_cores(self):
        cores = [self.c.submit(bytes([i]))["core"] for i in range(5)]
        self.assertEqual(cores, [0, 1, 0, 1, 0])
        self.assertEqual(len(self.node.calls("parameters")), 1)        # fetched once
        self.assertEqual(len(self.node.calls("servicePreimage")), 1)   # checked once

    def test_pinned_cores(self):
        c = chain.Jip2Chain(SID, self.node.url, timeout=5, authorizer=AUTH, cores=[1])
        try:
            self.assertEqual([c.submit(b"x")["core"] for _ in range(3)], [1, 1, 1])
        finally:
            c.rpc.close()

    def test_a_refused_core_falls_through_to_the_next(self):
        self.node.refuse[0] = RpcError(0, "queue full")
        r = self.c.submit(b"x")
        self.assertEqual(r["core"], 1)
        self.assertEqual(len(r["refused"]), 1)
        self.assertIn("core 0", r["refused"][0])
        self.assertEqual([s[0] for s in self.node.sent], [1])

    def test_every_core_refusing_is_busy_and_tries_each_once(self):
        self.node.refuse = {0: RpcError(0, "queue full"), 1: RpcError(0, "no guarantors")}
        with self.assertRaises(chain.ChainBusy) as cm:
            self.c.submit(b"x")
        self.assertIn("queue full", str(cm.exception))
        self.assertIn("no guarantors", str(cm.exception))
        self.assertEqual([r["params"][0] for r in self.node.calls("submitWorkPackage")], [0, 1])

    def test_a_node_without_submit_work_package(self):
        del self.node.methods["submitWorkPackage"]
        with self.assertRaises(chain.ChainUnsupported):
            self.c.submit(b"x")
        self.assertEqual(len(self.node.calls("submitWorkPackage")), 1)

    def test_a_dropped_submission_is_unknown_and_never_retried(self):
        self.node.drop_on.add("submitWorkPackage")
        with self.assertRaises(chain.ChainError) as cm:
            self.c.submit(b"x")
        self.assertNotIsInstance(cm.exception, (chain.ChainBusy, chain.ChainUnsupported))
        self.assertIn("outcome unknown", str(cm.exception))
        self.assertEqual(len(self.node.calls("submitWorkPackage")), 1)

    def test_a_failed_read_before_sending_is_busy(self):
        def unavailable(*_):
            raise RpcError(1, "Block unavailable")
        self.node.methods["beefyRoot"] = unavailable
        with self.assertRaisesRegex(chain.ChainBusy, "not sent"):
            self.c.submit(b"x")
        self.assertEqual(self.node.calls("submitWorkPackage"), [])

    def test_an_authorizer_the_chain_does_not_hold_is_refused_up_front(self):
        c = chain.Jip2Chain(SID, self.node.url, timeout=5,
                            authorizer=wp.Authorizer(0, b"\xee" * 32))
        try:
            with self.assertRaisesRegex(chain.ChainUnsupported, "holds no preimage"):
                c.submit(b"x")
        finally:
            c.rpc.close()
        self.assertEqual(self.node.calls("submitWorkPackage"), [])

    def test_a_node_without_service_preimage_leaves_the_authorizer_unchecked(self):
        del self.node.methods["servicePreimage"]
        self.assertEqual(self.c.submit(b"x")["core"], 0)

    def test_accumulate_gas_below_the_service_minimum(self):
        self.node.min_item_gas = G_A
        with self.assertRaisesRegex(chain.ChainUnsupported, "accumulate gas"):
            self.c.submit(b"x")
        self.assertEqual(self.node.calls("submitWorkPackage"), [])

    def test_explicit_gas_limits(self):
        c = chain.Jip2Chain(SID, self.node.url, timeout=5, authorizer=AUTH,
                            refine_gas=100_000_000, accumulate_gas=9_000_000)
        try:
            c.submit(b"x")
        finally:
            c.rpc.close()
        item, = wp.WorkPackage.decode(self.node.sent[-1][1]).items
        self.assertEqual((item.refine_gas, item.accumulate_gas), (100_000_000, 9_000_000))

    def test_at_genesis_the_best_block_is_the_anchor(self):
        for m in ("bestBlock", "finalizedBlock"):
            self.node.methods[m] = lambda: {"header_hash": B64(GRANDPARENT), "slot": 998}
        self.node.methods["beefyRoot"] = lambda hh: B64(b"\x13" * 32)
        self.c.submit(b"x")
        ctx = wp.WorkPackage.decode(self.node.sent[-1][1]).context
        self.assertEqual(ctx, wp.RefineContext(GRANDPARENT, 998, ROOTS[GRANDPARENT], b"\x13" * 32,
                                               GRANDPARENT, 998, ROOTS[GRANDPARENT]))

    def test_explicit_anchors(self):
        enc, ctx = self.c.work_package(b"x", chain.Block(1000, BEST, None))
        self.assertEqual(ctx, wp.RefineContext(BEST, 1000, ROOTS[BEST], BEEFY[BEST],
                                               GRANDPARENT, 998, ROOTS[GRANDPARENT]))
        self.assertEqual(wp.WorkPackage.decode(enc).context, ctx)
        _, ctx = self.c.work_package(b"x", chain.Block(1000, BEST, None), chain.Block(1000, BEST, None))
        self.assertEqual(ctx, wp.RefineContext(BEST, 1000, ROOTS[BEST], BEEFY[BEST],
                                               BEST, 1000, ROOTS[BEST]))
        self.assertEqual(self.node.sent, [])                  # built, not sent

    def test_lagging_finality_falls_back_to_the_anchor(self):
        # the finalized block's parent (slot 969) would be more than L - H slots older
        # than the anchor (999): it would age out before the report lands
        self.node.methods["finalizedBlock"] = lambda: {"header_hash": B64(OLD), "slot": 970}
        self.c.submit(b"x")
        ctx = wp.WorkPackage.decode(self.node.sent[-1][1]).context
        self.assertEqual((ctx.lookup_anchor, ctx.lookup_anchor_slot, ctx.lookup_anchor_state_root),
                         (PARENT, 999, ROOTS[PARENT]))
        # within the margin it is used as is
        SLOTS[OLD_PARENT] = 999 - (L - RECENT)
        try:
            self.c.submit(b"x")
        finally:
            SLOTS[OLD_PARENT] = 969
        ctx = wp.WorkPackage.decode(self.node.sent[-1][1]).context
        self.assertEqual((ctx.lookup_anchor, ctx.lookup_anchor_slot), (OLD_PARENT, 999 - (L - RECENT)))

    def test_package_status(self):
        r = self.c.submit(b"x")
        self.assertEqual(self.c.package_status(r), {"Reportable": {"remaining_blocks": 8}})
        self.assertEqual(self.node.calls("workPackageStatus")[-1]["params"],
                         [B64(BEST), B64(bytes.fromhex(r["package_hash"])), B64(PARENT)])
        self.c.package_status(r, at="final")
        self.assertEqual(self.node.calls("workPackageStatus")[-1]["params"][0], B64(PARENT))

    def test_no_service_id_is_a_configuration_error(self):
        self.c.service_id = None
        with self.assertRaisesRegex(chain.ChainError, "SERVICE_ID") as cm:
            self.c.submit(b"x")
        self.assertNotIsInstance(cm.exception, chain.ChainBusy)


class Configuration(unittest.TestCase):
    def setUp(self):
        self.node = FakeSubmitNode()

    def tearDown(self):
        self.node.stop()

    def test_no_authorizer_cannot_submit(self):
        c = chain.Jip2Chain(SID, self.node.url, timeout=5)
        self.assertFalse(c.submits)
        with self.assertRaisesRegex(chain.ChainUnsupported, "AUTHORIZER"):
            c.submit(b"x")
        self.assertEqual(self.node.requests, [])
        c.rpc.close()

    def test_the_authorizer_and_cores_from_a_chain_spec(self):
        # genesis: the authorizer's code is a preimage of service 0; only core 1's pool
        # holds H(H(code)) (empty configuration)
        spec = spec_with([[b"\x55" * 32], [H(H(AUTH_CODE))]], [(0, AUTH_CODE)])
        c = chain.Jip2Chain(SID, self.node.url, timeout=5, chain_spec=spec)
        try:
            self.assertTrue(c.submits)
            self.assertEqual([c.submit(b"x")["core"] for _ in range(2)], [1, 1])
            self.assertEqual(c.authorizer, AUTH)
        finally:
            c.rpc.close()

    def test_a_chain_spec_without_an_authorizer(self):
        c = chain.Jip2Chain(SID, self.node.url, timeout=5, chain_spec=spec_with([[b"\x55" * 32]], []))
        with self.assertRaisesRegex(chain.ChainUnsupported, "chain spec"):
            c.submit(b"x")
        c = chain.Jip2Chain(SID, self.node.url, timeout=5, chain_spec="/nonexistent/spec.json")
        with self.assertRaisesRegex(chain.ChainUnsupported, "chain spec"):
            c.submit(b"x")
        self.assertEqual(self.node.calls("submitWorkPackage"), [])

    def test_from_env_submission_nodes(self):
        c = chain.from_env({"CHAIN_BACKEND": "jip2", "CHAIN_RPC": "ws://rpc:1",
                            "CHAIN_SUBMIT_RPC": "gw=ws://rpc:1, ws://jj3:2"})
        self.assertEqual([n for n, _ in c._via], ["gw", "jj3:2"])
        self.assertIs(c._via[0][1], c.rpc)                  # the read node's connection
        self.assertEqual(c._via[1][1].url, "ws://jj3:2")
        self.assertIn("submits via gw, jj3:2", c.describe())
        self.assertEqual(chain.from_env({"CHAIN_BACKEND": "jip2"})._via[0][0], "localhost:19800")
        for bad in ("http://x:1", "gw=", "jj3:2"):
            with self.assertRaises(ValueError):
                chain.parse_endpoints(bad)

    def test_from_env(self):
        c = chain.from_env({"CHAIN_BACKEND": "jip2", "SERVICE_ID": "42",
                            "AUTHORIZER": f"0:{AUTH.code_hash.hex()}"})
        self.assertEqual((c.authorizer, c.chain_spec, c.submits), (AUTH, None, True))
        c = chain.from_env({"CHAIN_BACKEND": "jip2", "CHAIN_SPEC": "/etc/jam/spec.json"})
        self.assertEqual((c.authorizer, c.chain_spec, c.submits), (None, "/etc/jam/spec.json", True))
        self.assertFalse(chain.from_env({"CHAIN_BACKEND": "jip2"}).submits)
        with self.assertRaises(ValueError):
            chain.from_env({"CHAIN_BACKEND": "jip2", "AUTHORIZER": "0:beef"})


class SubmissionNodes(unittest.TestCase):
    """submit_via: packages through several nodes, one each in turn; reads from one."""
    def setUp(self):
        self.reads, self.a, self.b = FakeSubmitNode(), FakeSubmitNode(), FakeSubmitNode()
        self.c = chain.Jip2Chain(SID, self.reads.url, timeout=5, authorizer=AUTH,
                                 submit_via=[("a", self.a.url), ("b", self.b.url)])

    def tearDown(self):
        for _, node in self.c._via:
            node.close()
        self.c.rpc.close()
        for n in (self.reads, self.a, self.b):
            n.stop()

    def test_packages_alternate_nodes_and_reads_stay_on_one(self):
        vias = [self.c.submit(bytes([i]))["via"] for i in range(4)]
        self.assertEqual(vias, ["a", "b", "a", "b"])
        self.assertEqual((len(self.a.sent), len(self.b.sent), len(self.reads.sent)), (2, 2, 0))
        self.assertEqual(self.a.calls("bestBlock"), [])      # the package is built from `reads`
        self.assertTrue(self.reads.calls("bestBlock"))

    def test_each_node_takes_the_cores_in_turn(self):
        # two nodes in turn over two cores: not node a on core 0 and node b on core 1 forever
        got = [(r["via"], r["core"]) for r in (self.c.submit(bytes([i])) for i in range(4))]
        self.assertEqual(got, [("a", 0), ("b", 0), ("a", 1), ("b", 1)])

    def test_a_node_refusing_every_core_passes_the_package_on(self):
        self.a.refuse = {0: RpcError(0, "no guarantor"), 1: RpcError(0, "no guarantor")}
        r = self.c.submit(b"x")
        self.assertEqual((r["via"], r["core"]), ("b", 0))
        self.assertEqual([x.split(":")[0] for x in r["refused"]], ["a core 0", "a core 1"])
        self.assertEqual(self.c.submit(b"y")["via"], "b")    # next in turn after b: a refuses

    def test_an_unreachable_node_is_skipped_before_anything_is_sent(self):
        self.a.stop()
        self.c._via[0][1].close()
        r = self.c.submit(b"x")
        self.assertEqual(r["via"], "b")
        self.assertIn("a: unreachable", r["refused"][0])

    def test_every_node_refusing_is_busy(self):
        for n in (self.a, self.b):
            n.refuse = {0: RpcError(0, "full"), 1: RpcError(0, "full")}
        with self.assertRaisesRegex(chain.ChainBusy, "every node and core"):
            self.c.submit(b"x")
        self.assertEqual(len(self.a.calls("submitWorkPackage")) + len(self.b.calls("submitWorkPackage")), 4)

    def test_a_node_without_submission_passes_it_on(self):
        del self.a.methods["submitWorkPackage"]
        self.assertEqual(self.c.submit(b"x")["via"], "b")
        del self.b.methods["submitWorkPackage"]
        with self.assertRaises(chain.ChainUnsupported):
            self.c.submit(b"x")

    def test_a_dropped_submission_is_unknown_and_not_passed_on(self):
        self.a.drop_on.add("submitWorkPackage")
        with self.assertRaisesRegex(chain.ChainError, "via a: outcome unknown"):
            self.c.submit(b"x")
        self.assertEqual(self.b.calls("submitWorkPackage"), [])

    def test_for_service_keeps_the_submission_nodes(self):
        boot = self.c.for_service(0)
        self.assertEqual([n for n, _ in boot._via], ["a", "b"])
        self.assertIs(boot._via[1][1], self.c._via[1][1])


if __name__ == "__main__":
    unittest.main()
