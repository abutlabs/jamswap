"""Tests for the net configs: dev keys, the genesis node table, the compose generator.

    python3 -m unittest discover -s nets/tests
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import devkeys  # noqa: E402
import genesis  # noqa: E402
import netgen  # noqa: E402
import profiles  # noqa: E402
import soak  # noqa: E402

try:
    import yaml
except ImportError:                       # the generator itself needs only the stdlib
    yaml = None


class DevKeys(unittest.TestCase):
    def test_derivation_matches_the_published_table(self):
        # JIP-5 + RFC 8032 + JAMNP-S peer ids, against docs.jamcha.in/basics/dev-accounts
        for i, (name, ed, _, dns) in enumerate(devkeys.PUBLISHED):
            a = devkeys.dev_account(i)
            self.assertEqual((a["name"], a["ed25519"], a["peer_id"]), (name, ed, dns))

    def test_secret_seeds_are_jip5(self):
        # the table's ed25519_secret_seed / bandersnatch_secret_seed for Alice and Fergie
        self.assertEqual(devkeys.ed25519_secret_seed(devkeys.dev_seed(0)).hex(),
                         "996542becdf1e78278dc795679c825faca2e9ed2bf101bf3c4a236d3ed79cf59")
        self.assertEqual(devkeys.bandersnatch_secret_seed(devkeys.dev_seed(5)).hex(),
                         "75e73b8364bf4753c5802021c6aa6548cddb63fe668e3cacf7b48cdb6824bb09")

    def test_seed_is_u32le_index_repeated(self):
        self.assertEqual(devkeys.dev_seed(3).hex(), "03000000" * 8)

    def test_no_published_bandersnatch_beyond_tiny(self):
        with self.assertRaises(ValueError):
            devkeys.dev_account(6)


class Genesis(unittest.TestCase):
    def test_client_aliases(self):
        self.assertEqual(genesis.parse_clients(" lasair,pj, PolkaJam,jj,javajam,pbnjam "),
                         ["lasair", "polkajam", "polkajam", "javajam", "javajam", "pbnjam"])
        for bad in ("lasair,geth", "", ",,"):
            with self.assertRaises(ValueError):
                genesis.parse_clients(bad)

    def test_topology_keys_addresses_and_rows(self):
        clients = genesis.parse_clients("lasair,pj,pbnjam,javajam")
        vals, nodes = genesis.topology(clients, 41000, 42000, lambda i: "10.0.0.%d" % (10 + i))
        for i, v in enumerate(vals):
            acct = devkeys.dev_account(i)
            self.assertEqual(v, {"peer_id": acct["peer_id"], "bandersnatch": acct["bandersnatch"],
                                 "net_addr": "10.0.0.%d:%d" % (10 + i, 41000 + i)})
        # lasair and polkajam rows keep the shape their entrypoints read
        self.assertEqual(list(nodes[0]), ["index", "role", "host", "port", "peer_id", "identity", "own"])
        self.assertEqual(list(nodes[1]), ["index", "role", "host", "port", "rpc", "peer_id", "seed", "own"])
        self.assertEqual(nodes[2]["role"], "pbnjam")
        self.assertEqual((nodes[3]["role"], nodes[3]["rpc"]), ("javajam", 42003))
        self.assertEqual(genesis.bootnode(nodes), "%s@10.0.0.11:41001" % nodes[1]["peer_id"])

    def test_host_mode_puts_every_validator_at_the_host(self):
        _, nodes = genesis.topology(["polkajam", "javajam"], 41400, 42400, lambda i: "192.168.1.5")
        self.assertEqual({n["host"] for n in nodes}, {"192.168.1.5"})
        peers = genesis.peer_lists(nodes)
        self.assertEqual(peers[0], "192.168.1.5:41401@%s" % nodes[1]["peer_id"])

    def test_bootnode_without_polkajam_is_node_zero(self):
        _, nodes = genesis.topology(["javajam", "pbnjam"], 1, 2, lambda i: "1.2.3.4")
        self.assertTrue(genesis.bootnode(nodes).startswith(nodes[0]["peer_id"] + "@"))

    def test_genesis_accounts_custody_is_the_sum_of_balances(self):
        kv = genesis.genesis_accounts(5)
        for a in range(3):
            bal = sum(int.from_bytes(bytes.fromhex(v), "little") for k, v in kv.items()
                      if bytes.fromhex(k)[:1] == b"b" and bytes.fromhex(k)[1:5] == a.to_bytes(4, "little"))
            cust = int.from_bytes(bytes.fromhex(kv[(b"cust" + a.to_bytes(4, "little")).hex()]), "little")
            self.assertEqual((bal, cust), (6 * 5 * 10_000, 6 * 5 * 10_000))


GENERATED = [n for n in profiles.PROFILES if netgen.generated(n)]


class Netgen(unittest.TestCase):
    def test_committed_files_are_current(self):
        for n in GENERATED:
            with open(netgen.compose_path(n)) as f:
                self.assertEqual(f.read(), netgen.render(n), "%s is stale: ./dex gen" % n)

    def test_nets_do_not_collide(self):
        nets = [profiles.PROFILES[n]["net"] for n in GENERATED]
        self.assertEqual(len(nets), len(set(nets)))
        ports = [q for n in GENERATED for m in netgen.nodes(n) for q in (m["port"], m["rpc"]) if q]
        self.assertEqual(len(ports), len(set(ports)))
        # clear of the hand-written nets' host ports (19890, 19900, 40060, 40070, 8081, 8090)
        self.assertFalse({19890, 19900, 40060, 40070} & set(ports))

    def test_every_layout_is_tiny(self):
        for n in profiles.PROFILES:
            self.assertEqual(len(genesis.parse_clients(profiles.PROFILES[n]["clients"])), 6, n)

    def test_no_lasair_involved_without_a_lasair_node(self):
        for n in GENERATED:
            doc = netgen.compose(n)
            has = "lasair" in genesis.parse_clients(profiles.PROFILES[n]["clients"])
            init = doc["services"]["spec-init"]
            self.assertEqual(init["build"]["target"], "with-lasair" if has else "polkajam", n)
            if not has:
                self.assertNotIn("lasair", json.dumps(doc).replace("jamswap-polkajam", ""), n)
                self.assertFalse(netgen.dev_all_keys_default(n))

    def test_lasair_nodes_share_keys_only_among_lasair_indices(self):
        doc = netgen.compose("lasair-pj-javajam")
        for svc in ("lm0", "lm1"):
            env = doc["services"][svc]["environment"]
            self.assertEqual(env["GUARANTOR_OWN"], "${LASAIR_GUARANTOR_OWN-0,1}")
            self.assertEqual(env["WALL"], "1")          # wall-clock next to PolkaJam/JavaJAM
        self.assertTrue(netgen.dev_all_keys_default("lasair-pj-javajam"))

    def test_adapters(self):
        doc = netgen.compose("nolasair")["services"]
        self.assertEqual(doc["pj0"]["environment"]["FINALITY_MODE"], "grandpa")
        self.assertEqual(doc["pj0"]["ports"], ["${HOST_IP:-127.0.0.1}:41700:41700/udp",
                                               "127.0.0.1:42700:42700"])
        jj = doc["jj2"]
        self.assertEqual(jj["profiles"], ["javajam-docker"])
        self.assertIn("-Xmx${JAVAJAM_HEAP:-2g}", jj["environment"]["JAVA_TOOL_OPTIONS"])
        self.assertNotIn("-Xmx12g", jj["environment"]["JAVA_TOOL_OPTIONS"])
        self.assertEqual(jj["command"][:5], ["run", "--chain", "/shared/spec.json", "--dev-validator", "2"])
        self.assertEqual(doc["pb4"]["command"][:4], ["--chain", "/shared/spec.json", "--dev-validator", "4"])
        self.assertIn("@sha256:", doc["pb4"]["image"])
        self.assertIn("@sha256:", jj["image"])

    def test_dex_backend_follows_the_layout(self):
        self.assertEqual(netgen.dex_backend("lasair-pj-javajam"), "jamnp")   # lasair's bridges
        self.assertEqual(netgen.dex_backend("pj6"), "jip2")
        self.assertIsNone(netgen.dex_backend("pj-javajam"))

    def test_jip2_dex_deploys_at_startup_with_no_bridge(self):
        doc = netgen.compose("pj6")["services"]
        self.assertNotIn("builder", doc)
        self.assertNotIn("reader", doc)
        self.assertNotIn("SERVICE", doc["spec-init"]["environment"])     # nothing in genesis
        env = doc["dex"]["environment"]
        self.assertNotIn("SERVICE_ID", env)                               # deploy.py at startup
        self.assertEqual((env["CHAIN_BACKEND"], env["CHAIN_RPC"], env["CHAIN_SPEC"]),
                         ("jip2", "ws://rpc:42150", "/shared/spec.json"))
        self.assertEqual(env["SERVICE_CODE"], "/work/jamswap-service.jam")
        self.assertEqual(env["RESERVE_TOPUP"], "1")                       # the footprint is real
        self.assertIn("../../service/jamswap-service.jam:/work/jamswap-service.jam:ro",
                      doc["dex"]["volumes"])
        self.assertEqual(doc["dex"]["depends_on"]["rpc"], {"condition": "service_started"})
        self.assertEqual(doc["dex"]["ports"], ["8201:8080"])
        self.assertEqual(doc["loadgen"]["environment"]["DEX_URL"], "http://dex:8080")
        nw = doc["netwatch"]["environment"]
        self.assertEqual(nw["NETWATCH_NODES"].split(),
                         ["pj%d,polkajam,ws://pj%d:%d" % (i, i, 42100 + i) for i in range(6)])
        self.assertEqual(nw["NETWATCH_VALIDATORS"], "pj0,pj1,pj2,pj3,pj4,pj5")

    def test_jip2_dex_gateway_is_an_ordinary_polkajam_node(self):
        # a validator's RPC does not forward work-packages: the DEX has a node of its own
        profiles.PROFILES["_t"] = dict(clients="pbnjam,pj,pj,pj,pj,pj", net=98, finality="grandpa",
                                       dex=True, issue="-", about="-")
        try:
            doc = netgen.compose("_t")["services"]
            gw = doc["rpc"]
            self.assertEqual(gw["image"], doc["spec-init"]["image"])      # the net's PolkaJam build
            self.assertEqual(gw["environment"]["ROLE"], "node")          # no validator key
            self.assertEqual(gw["environment"]["FINALITY_MODE"], "grandpa")
            self.assertEqual((gw["environment"]["PORT"], gw["environment"]["RPC_PORT"]),
                             ("50850", "51850"))
            self.assertEqual(doc["dex"]["environment"]["CHAIN_RPC"], "ws://rpc:51850")
            # netwatch judges the validators, whatever their client
            self.assertEqual(doc["netwatch"]["environment"]["NETWATCH_NODES"].split()[0],
                             "pb0,pbnjam,ws://pb0:51800")
        finally:
            del profiles.PROFILES["_t"]

    def test_the_lasair_dex_stack_is_unchanged(self):
        doc = netgen.compose("lasair-pj-javajam")["services"]
        self.assertEqual(doc["dex"]["depends_on"], ["builder", "reader"])
        self.assertEqual(doc["dex"]["environment"]["SERVICE_ID"], "100")
        self.assertEqual(doc["spec-init"]["environment"]["SERVICE"], "/work/jamswap-service.jam")
        self.assertNotIn("loadgen", doc)

    @unittest.skipIf(yaml is None, "PyYAML not installed")
    def test_yaml_round_trip(self):
        for n in GENERATED:
            self.assertEqual(yaml.safe_load(netgen.render(n)), netgen.compose(n), n)


class SoakLoad(unittest.TestCase):
    """nets/soak.py: offered load the DEX turned away fails the soak (the SLO cannot see it)."""
    METRICS = "\n".join([
        "# HELP loadgen_ops_total operations offered to the dex API, by op",
        "# TYPE loadgen_ops_total counter",
        'loadgen_ops_total{op="buy"} 700', 'loadgen_ops_total{op="sealed_sell"} 140',
        'loadgen_ops_total{op="sell"} 560', 'loadgen_op_errors_total{op="buy"} 3',
        'loadgen_ops_busy_total{op="sell"} 1'])

    def test_counts(self):
        self.assertEqual(soak.load_counts(self.METRICS), {"offered": 1400, "refused": 3, "busy": 1})
        self.assertEqual(soak.load_counts(""), {"offered": 0, "refused": 0, "busy": 0})

    def test_turned_away_within_one_minus_target(self):
        c = soak.load_counts(self.METRICS)
        self.assertFalse(soak.judge_load(c, 0.9999)["pass"])     # 4 of 1400 > 0.14
        self.assertTrue(soak.judge_load(c, 0.99)["pass"])        # 4 of 1400 <= 14
        self.assertTrue(soak.judge_load(dict(c, refused=0, busy=0), 0.9999)["pass"])
        self.assertFalse(soak.judge_load({"offered": 0, "refused": 0, "busy": 0}, 0.9)["pass"])


if __name__ == "__main__":
    unittest.main()
