"""Runtime deploy through the Bootstrap service (offchain/deploy.py).

The CreateService encoding is checked byte for byte against two payloads `jamt 0.1.29
create-service` sent to a PolkaJam 0.1.29 dev node we ran (recorded off our own socket,
2026-09-26). The deploy flow runs against fake_jam.FakeJamNode, a fake chain whose
Bootstrap service parses the instruction with its own parser and runs GP `new`.

Run with:  python3 -m unittest discover -s offchain/tests
"""
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chain                                                      # noqa: E402
import deploy                                                     # noqa: E402
import workpackage as wp                                          # noqa: E402
from fake_jam import AUTH_CODE, FakeClock, FakeJamNode, H, parse_bootstrap   # noqa: E402

CODE = b"\x08\x00\x04demo\x010" + b"pvm code of the demo service"
REAL_BLOB = os.path.join(os.path.dirname(__file__), "..", "..", "service", "jamswap-service.jam")
JAMSWAP_HASH = bytes.fromhex("176c7886e12ff302c285c52d6c8cdd2921bd31015af46d6db5363a9920f98020")

# `jamt create-service --raw jamswap-service.jam 1000000000` (service id 1 was free)
JAMT_DEFAULT = bytes.fromhex(
    "01" "00" + JAMSWAP_HASH.hex()                       # one instruction, CreateService
    + "e378020000000000"                                 # code_len 162019
    + "1027000000000000" "1027000000000000"              # min item / memo gas 10000
    + "00ca9a3b00000000"                                 # endowment 1e9
    + "00" * 128                                         # memo (W_T = 128)
    + "00"                                               # registration: None
    + "0000000000000000"                                 # deposit offset 0
    + "01" "01000000"                                    # id: Some(1)
    + "e7822d052bf27b4896e41825664d96d3a12ff2d68e48e9dd5b00c001b8e92bf2")   # salt
# `jamt create-service --raw -G 11111 -g 22222 -i 4242 -r reg1 jamswap-service.jam 333 abc`
JAMT_OPTIONS = bytes.fromhex(
    "01" "00" + JAMSWAP_HASH.hex() + "e378020000000000"
    + "672b000000000000" "ce56000000000000"              # 11111, 22222
    + "4d01000000000000"                                 # 333
    + "616263" + "00" * 125                              # "abc"
    + "01" "04" "72656731"                               # Some(b"reg1")
    + "0000000000000000"
    + "01" "92100000"                                    # Some(4242)
    + "2e0ab539c336a5a779b5bc4aad459359a216625db66b842ee488d5079f4c0a61")


class Instruction(unittest.TestCase):
    def test_matches_what_jamt_sent(self):
        instr = deploy.CreateService(JAMSWAP_HASH, 162019, endowment=10 ** 9, service_id=1)
        self.assertEqual(deploy.bootstrap_payload([instr.encode()], JAMT_DEFAULT[-32:]), JAMT_DEFAULT)
        instr = deploy.CreateService(JAMSWAP_HASH, 162019, 11111, 22222, 333, b"abc",
                                     registration=b"reg1", service_id=4242)
        self.assertEqual(deploy.bootstrap_payload([instr.encode()], JAMT_OPTIONS[-32:]), JAMT_OPTIONS)

    def test_the_fake_parses_what_jamt_sent(self):
        # the fake chain's own parser agrees with the recording, so the flow tests below
        # check deploy.py against the recorded layout, not against itself
        (c,) = parse_bootstrap(JAMT_OPTIONS)
        self.assertEqual((c["code_hash"], c["code_len"], c["min_item_gas"], c["min_memo_gas"],
                          c["endowment"], c["memo"][:3], c["registration"], c["deposit_offset"], c["id"]),
                         (JAMSWAP_HASH, 162019, 11111, 22222, 333, b"abc", b"reg1", 0, 4242))

    def test_salt_is_random_and_32_bytes(self):
        instr = deploy.CreateService(bytes(32), 1).encode()
        a, b = deploy.bootstrap_payload([instr]), deploy.bootstrap_payload([instr])
        self.assertEqual(len(a), 2 + len(instr) - 1 + 32)
        self.assertNotEqual(a[-32:], b[-32:])
        with self.assertRaises(ValueError):
            deploy.bootstrap_payload([instr], b"short")

    def test_memo_and_hash_limits(self):
        with self.assertRaises(ValueError):
            deploy.CreateService(bytes(32), 1, memo=b"x" * 129).encode()
        with self.assertRaises(ValueError):
            deploy.CreateService(bytes(31), 1).encode()
        self.assertEqual(len(deploy.CreateService(bytes(32), 1, memo=b"m").encode(memo_size=4)),
                         1 + 32 + 32 + 4 + 1 + 8 + 1)


class CodeMetadata(unittest.TestCase):
    def test_the_real_blob(self):
        with open(REAL_BLOB, "rb") as f:
            blob = f.read()
        self.assertEqual(H(blob), JAMSWAP_HASH, "service/jamswap-service.jam changed: refresh the vectors")
        self.assertEqual(deploy.describe_code(blob), "jamswap-service 0.1.0")
        meta, code = deploy.split_code(blob)
        self.assertEqual((len(meta), len(code)), (35, len(blob) - 36))

    def test_unconventional_metadata(self):
        self.assertEqual(deploy.describe_code(b"\x02\x07\x01code"), "")
        self.assertEqual(deploy.describe_code(b"\x00code"), "")
        self.assertEqual(deploy.describe_code(b"\x7f"), "")


class Flow(unittest.TestCase):
    def setUp(self):
        self.node = FakeJamNode()
        self.t = FakeClock(self.node)
        self.ch = chain.Jip2Chain(None, self.node.url, timeout=5,
                                  authorizer=wp.Authorizer(0, H(AUTH_CODE)))
        self.dir = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.dir.name, "deploy.json")
        self.logs = []

    def tearDown(self):
        self.ch.rpc.close()
        self.node.stop()
        self.dir.cleanup()

    def saved(self):
        with open(self.state) as f:
            return json.load(f)

    def run_deploy(self, code=CODE, state=True, **kw):
        d = deploy.Deployer(self.ch, code, state_file=self.state if state else None,
                            log=self.logs.append, sleep=self.t.sleep, clock=self.t.clock)
        return d.run(**kw)

    def creates(self):
        return [parse_bootstrap(pkg.items[0].payload)[0] for _, pkg in self.node.packages_for(0)]

    def test_fresh_deploy(self):
        d = self.run_deploy()
        self.assertEqual((d.service_id, d.code_hash, d.reused), (1, H(CODE), False))
        self.assertEqual(self.ch.service_id, 1)
        (c,) = self.creates()
        self.assertEqual((c["code_hash"], c["code_len"], c["endowment"], c["id"], c["min_item_gas"]),
                         (H(CODE), len(CODE), deploy.DEFAULT_ENDOWMENT, 1, 10_000))
        (_, pkg), = self.node.packages_for(0)
        self.assertEqual(len(pkg.items), 1)
        self.assertEqual(pkg.items[0].code_hash, self.node.accounts[0].code_hash)
        self.assertEqual(self.node.preimages_sent, [(1, CODE)])
        acct = self.node.accounts[1]
        self.assertEqual(acct.preimages[H(CODE)], CODE)
        self.assertEqual(self.saved()["service_id"], 1)
        # usable: the code is at the lookup anchor the next package names
        lookup = self.ch.default_lookup_anchor(self.ch.default_anchor())
        self.assertIsNotNone(self.ch.preimage(1, H(CODE), at=lookup.hash))
        self.assertTrue(any("jam-bootstrap-service 0.1.29" in m for m in self.logs), self.logs)

    def test_rerun_reuses_through_the_state_file(self):
        self.run_deploy()
        sent = len(self.node.packages)
        d = self.run_deploy()
        self.assertEqual((d.service_id, d.reused), (1, True))
        self.assertEqual(len(self.node.packages), sent, "nothing submitted")
        self.assertEqual(len(self.node.preimages_sent), 1)

    def test_rerun_without_state_reuses_the_service_running_the_code(self):
        self.run_deploy(state=False)
        d = self.run_deploy(state=False)
        self.assertEqual((d.service_id, d.reused), (1, True))
        self.assertEqual(len(self.node.packages_for(0)), 1)

    def test_a_stale_state_file_is_ignored(self):
        with open(self.state, "w") as f:
            json.dump({"service_id": 9, "code_hash": H(CODE).hex()}, f)
        d = self.run_deploy()
        self.assertEqual((d.service_id, d.reused), (1, False))
        self.assertEqual(self.saved()["service_id"], 1)
        self.assertTrue(any("names service 9" in m for m in self.logs))

    def test_fresh_deploys_again_at_the_next_free_id(self):
        self.run_deploy()
        d = self.run_deploy(fresh=True)
        self.assertEqual((d.service_id, d.reused), (2, False))

    def test_a_failed_package_is_resubmitted(self):
        self.node.drop = 1
        d = self.run_deploy()
        self.assertEqual(d.service_id, 1)
        self.assertEqual(len(self.creates()), 2)
        self.assertEqual(sorted(self.node.accounts), [0, 1])

    def test_a_package_that_vanishes_is_resubmitted_once_its_anchor_is_old(self):
        self.node.lose = 1
        d = self.run_deploy()
        self.assertEqual(d.service_id, 1)
        (ph1, p1), (ph2, p2) = self.node.packages_for(0)
        # not before the first package's anchor has left recent history (H = 8) + grace
        self.assertGreaterEqual(p2.context.anchor_slot,
                                p1.context.anchor_slot + 8 + deploy.READY_GRACE_SLOTS)
        self.assertTrue(any("anchor aged out" in m for m in self.logs), self.logs)

    def test_a_bootstrap_that_is_not_the_registrar(self):
        # `new` ignores the id asked for and takes the next free public id
        self.node.bootstrap_is_registrar = False
        d = self.run_deploy()
        self.assertEqual(d.service_id, deploy.MIN_PUBLIC_ID + 7)
        self.assertEqual(self.node.preimages_sent, [(d.service_id, CODE)])

    def test_a_bootstrap_that_provides_the_code_itself(self):
        self.node.bootstrap_holds_code = True
        self.node.accounts[0].preimages[H(CODE)] = CODE
        self.assertEqual(self.run_deploy().service_id, 1)
        self.assertEqual(self.node.preimages_sent, [], "already provided: nothing to send")

    def test_an_explicit_id(self):
        self.assertEqual(self.run_deploy(service_id=77).service_id, 77)
        self.assertEqual(self.creates()[0]["id"], 77)
        d = self.run_deploy(service_id=77)                     # already runs our code
        self.assertEqual((d.service_id, d.reused), (77, True))
        with self.assertRaisesRegex(deploy.DeployError, "taken by other code"):
            self.run_deploy(code=CODE + b"v2", service_id=77)
        with self.assertRaisesRegex(deploy.DeployError, "must be in"):
            self.run_deploy(service_id=1 << 16)

    def test_skips_taken_ids(self):
        self.run_deploy(code=b"\x00other service")
        self.assertEqual(self.run_deploy().service_id, 2)

    def test_a_refused_create(self):
        self.node.bootstrap_refuses = True
        with self.assertRaisesRegex(deploy.DeployError, "created no service|no service with code"):
            self.run_deploy()

    def test_times_out(self):
        self.node.drop = 1000
        with self.assertRaisesRegex(deploy.DeployError, "timed out"):
            self.run_deploy(timeout=60)

    def test_needs_a_submitting_jip2_chain(self):
        with self.assertRaisesRegex(deploy.DeployError, "jip2"):
            deploy.deploy(chain.JamnpChain(None), CODE)
        with self.assertRaisesRegex(deploy.DeployError, "authorizer"):
            deploy.deploy(chain.Jip2Chain(None, self.node.url), CODE)

    def test_from_env(self):
        blob = os.path.join(self.dir.name, "svc.jam")
        with open(blob, "wb") as f:
            f.write(CODE)
        seen = {}

        def fake_deploy(ch, code, **kw):
            seen.update(kw, code=code)
            return deploy.Deployment(5, H(code), 1, False)
        real, deploy.deploy = deploy.deploy, fake_deploy
        try:
            deploy.from_env(self.ch, {"SERVICE_CODE": blob, "DEPLOY_STATE": self.state,
                                      "DEPLOY_SERVICE_ID": "0x10", "DEPLOY_FRESH": "1"})
        finally:
            deploy.deploy = real
        self.assertEqual((seen["code"], seen["state_file"], seen["service_id"], seen["fresh"],
                          seen["bootstrap_id"], seen["endowment"]),
                         (CODE, self.state, 16, True, 0, deploy.DEFAULT_ENDOWMENT))


class MultiItemPackages(unittest.TestCase):
    """Jip2Chain.submit_items: one package, one work-item per payload, gas shared."""
    def setUp(self):
        self.node = FakeJamNode()
        self.ch = chain.Jip2Chain(0, self.node.url, timeout=5,
                                  authorizer=wp.Authorizer(0, H(AUTH_CODE)))

    def tearDown(self):
        self.ch.rpc.close()
        self.node.stop()

    def test_items_in_order_with_shared_gas(self):
        self.ch.submit_items([b"a", b"bb", b"ccc"])
        (_, pkg), = self.node.packages
        self.assertEqual([w.payload for w in pkg.items], [b"a", b"bb", b"ccc"])
        self.assertEqual({(w.refine_gas, w.accumulate_gas) for w in pkg.items},
                         {((10 ** 9 - 1) // 3, (10 ** 7 - 1) // 3)})
        self.assertLess(sum(w.accumulate_gas for w in pkg.items), 10 ** 7)

    def test_limits(self):
        self.assertEqual(self.ch.max_items, 16)
        with self.assertRaises(chain.ChainUnsupported):
            self.ch.submit_items([b"x"] * 17)
        with self.assertRaises(chain.ChainUnsupported):
            self.ch.submit_items([])
        self.ch.accumulate_gas = 10 ** 6                       # explicit, per item
        with self.assertRaises(chain.ChainUnsupported):
            self.ch.submit_items([b"x"] * 10)                 # 10M >= G_A
        self.assertEqual(self.node.packages, [])

    def test_for_service_shares_the_connection(self):
        other = self.ch.for_service(7)
        self.assertIs(other.rpc, self.ch.rpc)
        self.assertEqual((other.service_id, other.authorizer), (7, self.ch.authorizer))

    def test_jamnp_takes_one_at_a_time(self):
        c = chain.JamnpChain(1, "http://127.0.0.1:9", "")
        self.assertEqual(c.max_items, 1)
        with self.assertRaises(chain.ChainUnsupported):
            c.submit_items([b"a", b"b"])


if __name__ == "__main__":
    unittest.main()
