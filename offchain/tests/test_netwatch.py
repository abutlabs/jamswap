"""netwatch (offchain/netwatch.py): the client-neutral poller, its verdict, the Prometheus
exporter and the state-parity probe; and soak_verdict's optional chain section.

JIP-2 nodes are fake_jip2.FakeJip2Node servers over an in-memory block tree; a node
without JIP-2 (lasair) is a fake HTTP server serving its Prometheus gauges and a CE-129
reader bridge. The live check against a stock PolkaJam testnet is manual (see the
commit message).

Run with:  python3 -m unittest discover -s offchain/tests
"""
import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import unittest.mock
import urllib.request
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(__file__)
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, ".."))
import netwatch as nw                            # noqa: E402
import soak_verdict                              # noqa: E402
from fake_jip2 import FakeJip2Node, RpcError     # noqa: E402

B64 = lambda b: base64.b64encode(b).decode()     # noqa: E731
VERDICT = os.path.join(HERE, "..", "soak_verdict.py")


def H(label):
    return hashlib.blake2b(str(label).encode(), digest_size=32).digest()


# ---- a fake chain behind a fake JIP-2 node ----------------------------------
class Tree:
    """Blocks by hash: (slot, parent hash)."""

    def __init__(self):
        self.blocks = {}

    def extend(self, parent, slots, tag):
        """Append blocks at `slots` after `parent` (None = a new genesis); their hashes."""
        out = []
        for s in slots:
            h = H(f"{tag}:{s}")
            self.blocks[h] = (s, parent)
            out.append(h)
            parent = h
        return out


class FakeNode:
    def __init__(self, tree, best, final, peers=5, stats=None, params=None, state=None):
        self.tree, self.best, self.final = tree, best, final
        self.peers, self.stats, self.params = peers, stats, params
        self.state = state or {}          # (header hash, key) -> value; absent = None
        self.fail = False
        methods = {"bestBlock": lambda: self._desc(self.best),
                   "finalizedBlock": lambda: self._desc(self.final),
                   "parent": self._parent, "syncState": self._sync,
                   "serviceValue": self._value}
        if stats is not None:
            methods["statistics"] = lambda h: B64(self.stats)
        if params is not None:
            methods["parameters"] = lambda: {"V1": self.params}
        self.srv = FakeJip2Node(methods)
        self.url = self.srv.url

    def _desc(self, h):
        if self.fail:
            raise RpcError(-32000, "down")
        return {"header_hash": B64(h), "slot": self.tree.blocks[h][0]}

    def _parent(self, h):
        slot, parent = self.tree.blocks[base64.b64decode(h)]
        if parent is None:
            raise RpcError(-32000, "no parent of the genesis block")
        return self._desc(parent)

    def _sync(self):
        return {"num_peers": self.peers, "status": "Completed"}

    def _value(self, h, sid, key):
        hh = base64.b64decode(h)
        if hh not in self.tree.blocks:
            raise RpcError(-32000, "unknown block")
        v = self.state.get((hh, base64.b64decode(key)))
        return None if v is None else B64(v)

    def stop(self):
        self.srv.stop()


class FakeLasair:
    """A lasair node without JIP-2: /metrics gauges and a reader bridge (/read)."""

    def __init__(self, slot, height, fslot, fheight):
        self.g = {"lasair_slot": slot, "lasair_block_height": height,
                  "lasair_finalized_slot": fslot, "lasair_finalized_height": fheight}
        self.store, self.heads = {}, ["aa" * 32]
        fake = self

        class Hd(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, data, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == "/metrics":
                    txt = "".join(f"{k} {v}\n" for k, v in fake.g.items())
                    self._reply(txt.encode(), "text/plain")
                elif u.path == "/read":
                    q = parse_qs(u.query)
                    v = fake.store.get(q["key"][0])
                    head = fake.heads.pop(0) if len(fake.heads) > 1 else fake.heads[0]
                    self._reply(json.dumps({"found": v is not None, "value_hex": v or "",
                                            "head_hex": head}).encode(), "application/json")
                else:
                    self.send_error(404)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), Hd)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def stats_blob(current, last, tail=b"\x00" * 17):
    enc = lambda recs: bytes([len(recs)]) + b"".join(struct.pack("<6I", *r) for r in recs)  # noqa: E731
    return enc(current) + enc(last) + tail


# ---- synthetic samples for the verdict ---------------------------------------
def smp(t, nodes, head=None, final=None, E=12, S=6, pi=None):
    """nodes: {name: (best_slot, best_hash, final_slot, final_hash[, up[, height, fheight]])}."""
    out = {}
    for n, v in nodes.items():
        bs, bh, fs, fh = v[:4]
        up = v[4] if len(v) > 4 else True
        bht, fht = (v[5], v[6]) if len(v) > 6 else (None, None)
        out[n] = {"client": "polkajam" if bh else "lasair", "kind": "jip2" if bh else "metrics",
                  "up": up, "best": {"slot": bs, "hash": bh, "height": bht} if up else None,
                  "final": {"slot": fs, "hash": fh, "height": fht} if up else None,
                  "peers": 5 if bh else None, "sync": None, "error": None if up else "down"}
    s = {"t": t, "epoch_slots": E, "slot_secs": S, "nodes": out}
    if head is not None:
        s["head"] = {"slot": head[0], "blocks": head[1]}
    if final is not None:
        s["final"] = {"slot": final[0], "blocks": final[1]}
    if pi is not None:
        s["pi"] = pi
    return s


def healthy(t, slot, names=("a", "b", "c")):
    h, f = f"h{slot}", f"h{slot - 2}"
    return smp(t, {n: (slot, h, slot - 2, f) for n in names},
               head=(slot, {n: {"slot": slot, "hash": h} for n in names}),
               final=(slot - 2, {n: {"slot": slot - 2, "hash": f} for n in names}))


class Nodes(unittest.TestCase):
    def test_parse(self):
        n = nw.parse_node("pj0, polkajam, ws://pj0:19800")
        self.assertEqual((n.name, n.client, n.url, n.reader, n.kind),
                         ("pj0", "polkajam", "ws://pj0:19800", None, "jip2"))
        n = nw.parse_node("lm0,lasair,http://lm0:9615/metrics,http://reader:19990/")
        self.assertEqual((n.kind, n.reader), ("metrics", "http://reader:19990"))
        for bad in ("pj0,ws://x", "a,b,ftp://x", ",c,ws://x", "a,b,ws://x,ws://y", "a,b,c,d,e"):
            with self.assertRaises(ValueError, msg=bad):
                nw.parse_node(bad)
        with self.assertRaises(ValueError):
            nw.parse_nodes(["a,c,ws://x", "a,c,ws://y"])

    def test_env(self):
        env = {"NETWATCH_NODES": "a,pj,ws://a:1; b,lasair,http://b:2/metrics  c,x,wss://c"}
        self.assertEqual([n.name for n in nw.nodes_from_env(env)], ["a", "b", "c"])
        # else the node the DEX's chain env already names
        n, = nw.nodes_from_env({"CHAIN_BACKEND": "jip2", "CHAIN_RPC": "ws://pj:19800"})
        self.assertEqual((n.url, n.kind, n.client), ("ws://pj:19800", "jip2", "jip2"))
        n, = nw.nodes_from_env({"NODE_METRICS_URL": "http://lm0:9615/metrics",
                                "READER_URL": "http://reader:19990/"})
        self.assertEqual((n.kind, n.client, n.reader), ("metrics", "lasair", "http://reader:19990"))
        self.assertEqual(nw.nodes_from_env({}), [])

    def test_validators(self):
        nodes = nw.parse_nodes(["pj0,polkajam,ws://a", "lm3,lasair,http://b"])
        self.assertEqual(nw.parse_validators("pj0,x:javajam,lm3", nodes),
                         [("pj0", "polkajam"), ("x", "javajam"), ("lm3", "lasair")])


class Statistics(unittest.TestCase):
    def test_general_natural(self):
        # GP serialization.tex: one prefix byte whose leading ones count the bytes after it
        for x, enc in ((0, "00"), (1, "01"), (127, "7f"), (128, "8080"), (0x3FFF, "bfff"),
                       (0x4000, "c00040"), (2 ** 56 - 1, "fe" + "ff" * 7),
                       (2 ** 64 - 1, "ff" + "ff" * 8)):
            self.assertEqual(nw.decode_nat(bytes.fromhex(enc)), (x, len(enc) // 2), x)

    def test_validator_statistics_as_polkajam_serves_them(self):
        # the live blob from a stock polkajam-testnet (nightly-2026-09-22) at slot 9118500,
        # the first slot of an epoch: pi_L's blocks sum to the 12-slot epoch
        live = bytes.fromhex(
            "06" + "00" * 72 + "01" + "00" * 23 + "00" * 48
            + "06" + "02000000" "03000000" + "00" * 16 + "01000000" + "00" * 20
            + "03000000" "02000000" + "00" * 16 + "02000000" "03000000" + "00" * 16
            + "01000000" + "00" * 20 + "03000000" "06000000" + "00" * 16 + "00" * 17)
        cur, last = nw.decode_validator_stats(live)
        self.assertEqual([r[0] for r in cur], [0, 0, 0, 1, 0, 0])
        self.assertEqual([r[0] for r in last], [2, 1, 3, 2, 1, 3])
        self.assertEqual(sum(r[0] for r in last), 12)
        self.assertEqual([r[1] for r in last], [3, 0, 2, 3, 0, 6])
        with self.assertRaises(ValueError):
            nw.decode_validator_stats(live[:100])

    def test_tracker_folds_each_finished_epoch_once(self):
        tr = nw.PiTracker()
        z = [0] * 6
        tr.update(24, 12, [[1] + z[1:], z], [[5] + z[1:], [5] + z[1:]])   # epoch 2 starts
        tr.update(30, 12, [[3] + z[1:], [1] + z[1:]], [[5] + z[1:], [5] + z[1:]])
        self.assertEqual({i: r[0] for i, r in tr.credited().items()}, {0: 3, 1: 1})
        tr.update(36, 12, [z, z], [[4] + z[1:], [2] + z[1:]])   # epoch 3: fold epoch 2's finals
        tr.update(37, 12, [[1] + z[1:], z], [[4] + z[1:], [2] + z[1:]])
        tr.update(30, 12, [[9] + z[1:], z], [[9] + z[1:], z])   # an older sample is ignored
        self.assertEqual({i: r[0] for i, r in tr.credited().items()}, {0: 5, 1: 2})


class Sampling(unittest.TestCase):
    """The sampler against fake nodes: reads, alignment by parent(), the lasair gauges."""

    def setUp(self):
        self.tree = Tree()
        self.main = self.tree.extend(None, range(100, 111), "main")      # slots 100..110
        self.fork = self.tree.extend(self.main[5], [106, 108], "fork")   # off slot 105
        self.nodes = []

    def tearDown(self):
        for n in self.nodes:
            n.stop()

    def node(self, *a, **k):
        n = FakeNode(self.tree, *a, **k)
        self.nodes.append(n)
        return n

    def lasair(self, *a):
        n = FakeLasair(*a)
        self.nodes.append(n)
        return n

    def sampler(self, specs, **kw):
        s = nw.Sampler(nw.parse_nodes(specs), timeout=5, **kw)
        self.addCleanup(s.close)
        return s

    def test_one_chain_nodes_a_slot_apart_agree(self):
        a = self.node(self.main[10], self.main[8])                      # best 110, final 108
        b = self.node(self.main[9], self.main[7])                       # best 109, final 107
        s = self.sampler([f"a,polkajam,{a.url}", f"b,javajam,{b.url}"]).sample()
        self.assertEqual(s["head"]["slot"], 109)
        self.assertEqual(s["head"]["blocks"]["a"], {"slot": 109, "hash": self.main[9].hex()})
        self.assertEqual(s["final"]["blocks"]["a"], {"slot": 107, "hash": self.main[7].hex()})
        self.assertEqual(s["nodes"]["b"]["peers"], 5)
        j = nw.judge_sample(s)
        self.assertTrue(j["ok"], j)
        self.assertEqual((j["heads"], j["final_heads"], j["lag"]), (1, 1, {"a": 0, "b": 1}))

    def test_a_fork_is_two_heads(self):
        a = self.node(self.main[10], self.main[4])
        b = self.node(self.fork[1], self.main[4])                       # fork tip at slot 108
        j = nw.judge_sample(self.sampler([f"a,pj,{a.url}", f"b,pj,{b.url}"]).sample())
        self.assertEqual((j["ok"], j["split"], j["heads"]), (False, True, 2))

    def test_empty_slots_align_to_the_newest_block_at_or_below(self):
        a = self.node(self.fork[1], self.main[4])                       # fork: 105, 106, 108
        b = self.node(self.fork[1], self.main[4])
        c = self.node(self.fork[0], self.main[4])                       # at 106
        s = self.sampler([f"a,pj,{a.url}", f"b,pj,{b.url}", f"c,pj,{c.url}"]).sample()
        self.assertEqual(s["head"]["slot"], 106)
        self.assertTrue(nw.judge_sample(s)["ok"])

    def test_a_node_too_far_behind_to_align_is_lag(self):
        a = self.node(self.main[10], self.main[0])
        b = self.node(self.main[0], self.main[0])
        s = self.sampler([f"a,pj,{a.url}", f"b,pj,{b.url}"], max_lag=2).sample()
        self.assertIsNone(s["head"]["blocks"]["a"], "10 slots > the 2*2+4 hop bound")
        j = nw.judge_sample(s, 2)
        self.assertEqual((j["ok"], j["unaligned"], j["lagging"]), (False, ["a"], ["b"]))

    def test_a_down_node_does_not_stop_the_sample(self):
        a = self.node(self.main[10], self.main[8])
        b = self.node(self.main[10], self.main[8])
        b.fail = True
        s = self.sampler([f"a,pj,{a.url}", f"b,pj,{b.url}"]).sample()
        self.assertFalse(s["nodes"]["b"]["up"])
        self.assertIn("down", s["nodes"]["b"]["error"])
        self.assertEqual(nw.judge_sample(s)["down"], ["b"])

    def test_lasair_by_its_gauges_beside_jip2(self):
        a = self.node(self.main[10], self.main[8])
        lm = self.lasair(110, 57, 108, 55)
        s = self.sampler([f"a,polkajam,{a.url}", f"lm0,lasair,{lm.base}/metrics"]).sample()
        self.assertEqual(s["nodes"]["lm0"]["best"], {"slot": 110, "hash": None, "height": 57})
        self.assertEqual(s["nodes"]["lm0"]["final"], {"slot": 108, "hash": None, "height": 55})
        self.assertEqual(list(s["head"]["blocks"]), ["a"], "only JIP-2 nodes are hash-aligned")
        self.assertTrue(nw.judge_sample(s)["ok"])

    def test_two_lasair_heights_at_one_slot_are_a_split(self):
        l1, l2 = self.lasair(110, 57, 100, 50), self.lasair(110, 58, 100, 50)
        s = self.sampler([f"l1,lasair,{l1.base}/metrics", f"l2,lasair,{l2.base}/metrics"]).sample()
        self.assertIsNone(s["head"])
        j = nw.judge_sample(s)
        self.assertEqual((j["split"], j["height_split"]), (True, [110]))

    def test_parameters_and_statistics(self):
        blob = stats_blob([(1, 0, 0, 0, 0, 0), (0,) * 6], [(7, 1, 0, 0, 2, 3), (5,) * 6])
        a = self.node(self.main[10], self.main[8], stats=blob,
                      params={"epoch_period": 600, "slot_period_sec": 6})
        s = self.sampler([f"a,pj,{a.url}"]).sample()
        self.assertEqual((s["epoch_slots"], s["slot_secs"]), (600, 6))
        self.assertEqual(s["pi"]["last"], [[7, 1, 0, 0, 2, 3], [5] * 6])
        self.assertEqual(s["pi"]["node"], "a")
        # flags win over parameters(); without either, the tiny default
        b = self.node(self.main[10], self.main[8])
        self.assertEqual(self.sampler([f"b,pj,{b.url}"], epoch_slots=30, slot_secs=2).sample()["epoch_slots"], 30)
        self.assertEqual(self.sampler([f"b,pj,{b.url}"]).sample()["epoch_slots"], nw.DEFAULT_EPOCH_SLOTS)
        self.assertNotIn("pi", self.sampler([f"b,pj,{b.url}"]).sample(), "no statistics served")


class Verdict(unittest.TestCase):
    def run_of(self, n=20, start=1000, names=("a", "b", "c")):
        return [healthy(1000.0 + 6 * i, start + i, names) for i in range(n)]

    def test_a_healthy_run_passes(self):
        v = nw.verdict(self.run_of(), finality="require")
        self.assertTrue(v["pass"], v)
        self.assertEqual(v["one_head"]["ok_samples"], 20)
        self.assertEqual(v["finality"]["status"], "finalizing")
        self.assertEqual(v["liveness"]["advance_slots"], {"a": 19, "b": 19, "c": 19})
        self.assertIn("hash", v["one_head"]["method"])

    def split_at(self, s, node="c"):
        s["head"]["blocks"][node] = {"slot": s["head"]["slot"], "hash": "other"}
        return s

    def test_a_fork_shorter_than_an_epoch_passes_and_a_longer_one_fails(self):
        run = self.run_of(30)
        for s in run[5:10]:                    # 4 slots of progress, 24 s: within 12 slots
            self.split_at(s)
        v = nw.verdict(run)
        self.assertTrue(v["one_head"]["pass"], v["one_head"])
        self.assertEqual((v["one_head"]["episodes"], v["one_head"]["longest_episode_slots"]), (1, 4))
        run = self.run_of(30)
        for s in run[5:20]:                    # 14 slots > one 12-slot epoch
            self.split_at(s)
        v = nw.verdict(run)
        self.assertFalse(v["pass"])
        ep, = v["one_head"]["failed_episodes"]
        self.assertEqual((ep["kinds"], ep["nodes"], ep["slots"]), (["split"], ["c"], 14))

    def test_a_long_fork_while_the_chain_stands_still_is_measured_in_wall_time(self):
        run = [self.split_at(healthy(1000.0 + 6 * i, 1000)) for i in range(20)]
        v = nw.verdict(run)
        self.assertFalse(v["one_head"]["pass"])
        self.assertFalse(v["liveness"]["pass"], "nobody's best advanced")

    def test_lag_beyond_the_bound_for_over_an_epoch_fails(self):
        run = self.run_of(30)
        for s in run[3:20]:
            s["nodes"]["c"]["best"]["slot"] -= 5
        self.assertFalse(nw.verdict(run, max_lag=3)["one_head"]["pass"])
        self.assertTrue(nw.verdict(run, max_lag=5)["one_head"]["pass"], "within the bound")

    def test_a_node_down_for_over_an_epoch_fails(self):
        run = self.run_of(30)
        for s in run[2:18]:
            s["nodes"]["b"].update(up=False, best=None, final=None)
        v = nw.verdict(run)
        self.assertFalse(v["one_head"]["pass"])
        self.assertEqual(v["one_head"]["failed_episodes"][0]["kinds"], ["down"])

    def test_a_run_that_never_agreed_fails_even_if_short(self):
        run = [self.split_at(s) for s in self.run_of(3)]
        self.assertFalse(nw.verdict(run)["one_head"]["pass"])

    def test_finality_conflict_regression_and_stall(self):
        run = self.run_of(20)
        run[7]["nodes"]["b"]["final"]["hash"] = "evil"               # same slot, other hash
        v = nw.verdict(run)
        self.assertFalse(v["finality"]["pass"])
        self.assertEqual(v["finality"]["conflicts"][0]["slot"], run[7]["nodes"]["b"]["final"]["slot"])

        run = self.run_of(20)
        run[9]["nodes"]["a"]["final"]["slot"] = 900                  # went back
        run[9]["final"]["blocks"].pop("a")
        v = nw.verdict(run)
        self.assertEqual([r["node"] for r in v["finality"]["regressions"]], ["a"])
        self.assertFalse(v["pass"])

        run = self.run_of(40)
        for s in run[10:30]:                                         # finality frozen 20 slots
            for n in s["nodes"].values():
                n["final"] = {"slot": 1008, "hash": "h1008", "height": None}
            s["final"] = {"slot": 1008, "blocks": {k: {"slot": 1008, "hash": "h1008"} for k in "abc"}}
        v = nw.verdict(run)
        self.assertEqual(v["finality"]["stalled"], ["a", "b", "c"])
        self.assertFalse(v["finality"]["pass"])
        self.assertTrue(nw.verdict(run, final_stall_slots=25)["finality"]["pass"])

    def test_a_restart_may_reset_finality(self):
        run = self.run_of(20)
        run[8]["nodes"]["a"].update(up=False, best=None, final=None)
        run[9]["nodes"]["a"]["final"]["slot"] = 0                     # rebooted, not caught up
        run[9]["final"]["blocks"].pop("a")
        v = nw.verdict(run, max_lag=3)
        self.assertEqual(v["finality"]["regressions"], [])

    def test_a_net_that_does_not_finalize(self):
        run = self.run_of(10)
        for s in run:
            for n in s["nodes"].values():
                n["final"] = {"slot": 0, "hash": "g", "height": None}
            s["final"] = {"slot": 0, "blocks": {k: {"slot": 0, "hash": "g"} for k in "abc"}}
        v = nw.verdict(run)
        self.assertEqual((v["finality"]["status"], v["finality"]["pass"], v["pass"]),
                         ("not finalizing", True, True))
        self.assertFalse(nw.verdict(run, finality="require")["pass"])

    def test_finality_on_some_nodes_only_fails_unless_only_reported(self):
        run = self.run_of(10)
        for s in run:
            s["nodes"]["c"]["final"] = {"slot": 0, "hash": "g", "height": None}
            s["final"]["blocks"]["c"] = None
        v = nw.verdict(run)
        self.assertEqual(v["finality"]["not_advanced"], ["c"])
        self.assertFalse(v["pass"])
        # a mixed net whose clients do not share finality: reported, not judged
        v = nw.verdict(run, finality="report")
        self.assertEqual((v["pass"], v["finality"]["pass"], v["finality"]["judged"]), (True, False, False))
        buf = io.StringIO()
        nw.print_verdict(v, buf)
        self.assertIn("finality            : report only: FAIL", buf.getvalue())
        with self.assertRaises(ValueError):
            nw.verdict(run, finality="maybe")

    def test_nodes_without_hashes_by_slot_and_height(self):
        run = [smp(1000.0 + 6 * i, {n: (500 + i, None, 498 + i, None, True, 50 + i, 48 + i) for n in "xy"})
               for i in range(10)]
        v = nw.verdict(run, finality="require")
        self.assertTrue(v["pass"], v)
        self.assertIn("slot/height only", v["one_head"]["method"])
        run[4]["nodes"]["y"]["final"]["height"] = 99                  # same finalized slot, other height
        self.assertEqual(nw.verdict(run)["finality"]["conflicts"][0]["slot"], 502)

    def test_authoring_and_peers(self):
        z = [0] * 6
        run = self.run_of(4)
        run[0]["pi"] = {"node": "a", "slot": 1000, "current": [[1] + z[1:], z, z], "last": [z, z, z]}
        run[3]["pi"] = {"node": "a", "slot": 1003, "current": [[2] + z[1:], [1] + z[1:], z], "last": [z, z, z]}
        v = nw.verdict(run, require_authoring=True)
        self.assertEqual((v["authoring"]["blocks"], v["authoring"]["idle"]), ({"0": 2, "1": 1, "2": 0}, ["2"]))
        self.assertFalse(v["pass"])
        self.assertTrue(nw.verdict(run)["pass"], "only judged when required")
        self.assertFalse(nw.verdict(self.run_of(4), require_authoring=True)["pass"], "no statistics")
        v = nw.verdict(self.run_of(4), min_peers=6)
        self.assertEqual((v["peers"]["pass"], v["peers"]["below"]), (False, ["a", "b", "c"]))

    def test_no_samples(self):
        self.assertFalse(nw.verdict([])["pass"])

    def test_load_samples_skips_a_torn_line_and_verdict_json_passes_through(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "s.jsonl")
        with open(p, "w") as fh:
            fh.write("".join(json.dumps(s) + "\n" for s in self.run_of(5)) + '{"t": 1, "no')
        self.assertEqual(len(nw.load_samples(p)), 5)
        self.assertTrue(nw.load_chain_report(p)["pass"])
        q = os.path.join(d, "v.json")
        with open(q, "w") as fh:
            json.dump({"pass": False, "one_head": {}}, fh)
        self.assertEqual(nw.load_chain_report(q), {"pass": False, "one_head": {}})


class Exporter(unittest.TestCase):
    def test_metrics_and_verdict_over_http(self):
        tree = Tree()
        main = tree.extend(None, range(100, 111), "m")
        fork = tree.extend(main[8], [110], "f")
        blob = stats_blob([(1, 0, 0, 0, 0, 0)] * 2, [(6, 0, 0, 0, 0, 0)] * 2)
        a = FakeNode(tree, main[10], main[8], stats=blob, peers=7)
        b = FakeNode(tree, fork[0], main[7])
        lm = FakeLasair(109, 60, 107, 58)
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        self.addCleanup(lm.stop)
        nodes = nw.parse_nodes([f"a,polkajam,{a.url}", f"b,javajam,{b.url}", f"lm0,lasair,{lm.base}/metrics"])
        s = nw.Sampler(nodes, timeout=5, validators=nw.parse_validators("a,lm0", nodes))
        self.addCleanup(s.close)
        ex = nw.Exporter(s)
        _, j = ex.step()
        self.assertTrue(j["split"])
        srv = nw.serve(ex, 0, 3600)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        base = "http://127.0.0.1:%d" % srv.server_address[1]
        txt = urllib.request.urlopen(base + "/metrics").read().decode()
        want = ['jam_best_slot{node="a",client="polkajam"} 110',
                'jam_best_slot{node="lm0",client="lasair"} 109',
                'jam_best_height{node="lm0",client="lasair"} 60',
                'jam_finalized_slot{node="b",client="javajam"} 107',
                'jam_finalized_height{node="lm0",client="lasair"} 58',
                'jam_peers{node="a",client="polkajam"} 7',
                'jam_head_lag_slots{node="lm0",client="lasair"} 1',
                'jam_finality_lag_slots{node="a",client="polkajam"} 2',
                'jam_best_hash48{node="a",client="polkajam"} %d' % int(main[10][:6].hex(), 16),
                "jam_net_heads 2", "jam_net_one_head 0", "jam_net_nodes 3", "jam_net_nodes_up 3",
                "jam_net_head_slot 110", "jam_net_finalized_slot 107",
                'jam_pi_blocks{validator="0",node="a",client="polkajam",epoch="last"} 6',
                'jam_pi_blocks{validator="1",node="lm0",client="lasair",epoch="current"} 1',
                'jam_pi_blocks_cumulative_total{validator="1",node="lm0",client="lasair"} 0',
                "# TYPE jam_net_samples_total counter"]
        for w in want:
            self.assertIn(w, txt)
        # the two JIP-2 nodes split 1:1 at the common slot: no majority, neither agrees
        self.assertIn('jam_head_agree{node="a",client="polkajam"} 0', txt)
        self.assertNotIn('jam_head_agree{node="lm0"', txt, "no hash to agree with")
        v = json.loads(urllib.request.urlopen(base + "/verdict").read())
        self.assertFalse(v["one_head"]["pass"])
        self.assertEqual(urllib.request.urlopen(base + "/verdict").status, 200)

    def test_finality_conflicts_are_counted(self):
        s = nw.Sampler([nw.Node("a", "x", "ws://127.0.0.1:1")])
        self.addCleanup(s.close)
        ex = nw.Exporter(s)
        seq = [healthy(1.0, 50, "ab"), healthy(2.0, 51, "ab")]
        seq[1]["nodes"]["b"]["final"] = {"slot": 48, "hash": "evil", "height": None}
        s.sample = lambda: seq.pop(0)
        ex.step()
        ex.step()
        self.assertIn("jam_finality_conflicts_total 1", ex.render())


class Parity(unittest.TestCase):
    SID = 7

    def setUp(self):
        self.tree = Tree()
        self.main = self.tree.extend(None, range(10, 20), "p")
        self.keys = nw.service_keys(markets=[1], accounts=[1, 2], assets=[0])
        book = struct.pack("<IIBII", 1, 5, 0, 100, 10)
        self.state = {}
        for blk in self.main:                                 # the same state at every block
            self.state[(blk, b"book" + struct.pack("<I", 1))] = book
            self.state[(blk, b"b" + struct.pack("<II", 0, 1))] = struct.pack("<Q", 1000)
        self.nodes = []

    def tearDown(self):
        for n in self.nodes:
            n.stop()

    def node(self, best, final, state=None):
        n = FakeNode(self.tree, best, final, state=dict(self.state if state is None else state))
        self.nodes.append(n)
        return n

    def probes(self, specs):
        ps = [nw.Probe(n, 5) for n in nw.parse_nodes(specs)]
        self.addCleanup(lambda: [p.close() for p in ps])
        return ps

    def test_keys(self):
        k = nw.service_keys(markets=[2], accounts=[3], assets=[1], rounds=[b"\x01" * 32], extra=[b"zz", b"markets"])
        self.assertIn(b"book" + struct.pack("<I", 2), k)
        self.assertIn(b"b" + struct.pack("<II", 1, 3), k)
        self.assertIn(b"b" + struct.pack("<II", 1, nw.TREASURY), k)
        self.assertNotIn(b"pk" + struct.pack("<I", nw.TREASURY), k)
        self.assertIn(b"rl" + b"\x01" * 32, k)
        self.assertEqual(k.count(b"markets"), 1)
        self.assertEqual(k[-1], b"zz")

    def test_digest_tells_absent_from_empty(self):
        self.assertNotEqual(nw.state_digest([(b"k", None)]), nw.state_digest([(b"k", b"")]))
        self.assertNotEqual(nw.state_digest([(b"ab", b"c")]), nw.state_digest([(b"a", b"bc")]))

    def test_agree_at_the_lowest_finalized_head(self):
        a = self.node(self.main[9], self.main[7])
        b = self.node(self.main[8], self.main[5])
        r = nw.parity(self.probes([f"a,polkajam,{a.url}", f"b,javajam,{b.url}"]), self.SID, self.keys)
        self.assertTrue(r["pass"], r)
        self.assertEqual((r["at"]["slot"], r["at"]["hash"], r["at"]["from"]), (15, self.main[5].hex(), "b"))
        self.assertEqual(r["nodes"]["a"]["present"], 2)
        self.assertEqual(len(r["groups"]), 1)
        reads = [q for q in a.srv.requests if q["method"] == "serviceValue"]
        self.assertEqual(len(reads), len(self.keys))
        self.assertTrue(all(q["params"][:2] == [B64(self.main[5]), self.SID] for q in reads))

    def test_a_divergence_names_the_key_and_is_not_retried(self):
        a = self.node(self.main[9], self.main[7])
        bad = dict(self.state)
        bad[(self.main[7], b"b" + struct.pack("<II", 0, 1))] = struct.pack("<Q", 999)
        b = self.node(self.main[9], self.main[7], state=bad)
        r = nw.parity(self.probes([f"a,pj,{a.url}", f"b,jj,{b.url}"]), self.SID, self.keys, pause=0)
        self.assertFalse(r["pass"])
        self.assertEqual(r["attempt"], 1, "a pinned mismatch is final")
        key = (b"b" + struct.pack("<II", 0, 1)).hex()
        self.assertEqual(list(r["mismatched_keys"]), [key])
        self.assertEqual(r["mismatched_keys"][key]["b"], struct.pack("<Q", 999).hex())

    def test_one_node_alone_proves_nothing(self):
        a = self.node(self.main[9], self.main[7])
        r = nw.parity(self.probes([f"a,pj,{a.url}"]), self.SID, self.keys)
        self.assertFalse(r["pass"])
        self.assertIn("nothing to compare", r["reason"])

    def test_a_node_that_cannot_read_fails(self):
        a = self.node(self.main[9], self.main[7])
        other = Tree()
        lone = other.extend(None, [30, 31], "x")
        b = FakeNode(other, lone[1], lone[0])
        self.nodes.append(b)
        r = nw.parity(self.probes([f"a,pj,{a.url}", f"b,pj,{b.url}"]), self.SID, self.keys,
                      attempts=2, pause=0)
        self.assertFalse(r["pass"])
        self.assertIn("errors on", r["reason"])
        self.assertIn("unknown block", r["nodes"]["b" if r["at"]["from"] == "a" else "a"]["error"])

    def test_lasair_through_its_reader_head(self):
        a = self.node(self.main[9], self.main[7])
        lm = FakeLasair(19, 9, 17, 7)
        self.nodes.append(lm)
        lm.store = {k.hex(): v.hex() for (blk, k), v in self.state.items() if blk == self.main[0]}
        lm.heads = ["01" * 32, "02" * 32, "02" * 32]          # the head moves during the first read
        r = nw.parity(self.probes([f"a,pj,{a.url}", f"lm0,lasair,{lm.base}/metrics,{lm.base}"]),
                      self.SID, self.keys, pause=0)
        self.assertTrue(r["pass"], r)
        self.assertEqual((r["nodes"]["lm0"]["pinned"], r["nodes"]["lm0"]["at"]), (False, "02" * 32))

    def test_an_unpinned_mismatch_is_retried(self):
        a = self.node(self.main[9], self.main[7])
        lm = FakeLasair(19, 9, 17, 7)
        self.nodes.append(lm)
        calls = []
        orig = nw.Probe.read_keys_at_reader_head

        def flaky(probe, sid, keys, attempts=3):
            calls.append(1)
            pairs, head = orig(probe, sid, keys, attempts)
            if len(calls) == 1:                              # first attempt: state not there yet
                return [(k, None) for k, _ in pairs], head
            return pairs, head
        lm.store = {k.hex(): v.hex() for (blk, k), v in self.state.items() if blk == self.main[0]}
        with unittest.mock.patch.object(nw.Probe, "read_keys_at_reader_head", flaky):
            r = nw.parity(self.probes([f"a,pj,{a.url}", f"lm0,lasair,{lm.base}/metrics,{lm.base}"]),
                          self.SID, self.keys, pause=0)
        self.assertEqual((r["pass"], r["attempt"]), (True, 2))

    def test_a_node_without_its_own_reader_is_skipped_not_failed(self):
        a = self.node(self.main[9], self.main[7])
        b = self.node(self.main[9], self.main[7])
        lm = FakeLasair(19, 9, 17, 7)
        self.nodes.append(lm)
        lm.store = {k.hex(): v.hex() for (blk, k), v in self.state.items() if blk == self.main[0]}
        r = nw.parity(self.probes([f"a,pj,{a.url}", f"b,pj,{b.url}",
                                   f"lm0,lasair,{lm.base}/metrics,{lm.base}",
                                   f"lm1,lasair,{lm.base}/metrics,{lm.base}",     # lm0's reader
                                   f"lm2,lasair,{lm.base}/metrics"]), self.SID, self.keys)
        self.assertTrue(r["pass"], r)
        self.assertEqual(sorted(r["nodes"]), ["a", "b", "lm0"])
        self.assertIn("lm0's", r["skipped"]["lm1"])
        self.assertIn("no reader bridge", r["skipped"]["lm2"])

    def test_a_down_jip2_node_fails_only_itself(self):
        a = self.node(self.main[9], self.main[7])
        b = self.node(self.main[9], self.main[6])
        c = self.node(self.main[9], self.main[7])
        c.fail = True
        r = nw.parity(self.probes([f"a,pj,{a.url}", f"b,pj,{b.url}", f"c,pj,{c.url}"]), self.SID,
                      self.keys, attempts=1)
        self.assertFalse(r["pass"])
        self.assertEqual((r["at"]["from"], r["reason"]), ("b", "errors on c"))
        self.assertEqual(r["nodes"]["a"]["digest"], r["nodes"]["b"]["digest"])

    def test_cli(self):
        a = self.node(self.main[9], self.main[7])
        b = self.node(self.main[9], self.main[6])
        out = tempfile.mktemp(suffix=".json")
        self.addCleanup(lambda: os.path.exists(out) and os.remove(out))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = nw.main(["parity", "--node", f"a,pj,{a.url}", "--node", f"b,pj,{b.url}",
                            "--service", str(self.SID), "--markets", "1", "--accounts", "1-2",
                            "--assets", "0", "--round", "ab" * 32, "--out", out])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("state parity        : PASS", buf.getvalue())
        with open(out) as fh:
            self.assertEqual(json.load(fh)["keys"], len(self.keys) + 1)


class Cli(unittest.TestCase):
    def test_poll_writes_samples_and_judges(self):
        tree = Tree()
        main = tree.extend(None, range(1, 5), "c")
        a, b = FakeNode(tree, main[3], main[1]), FakeNode(tree, main[3], main[1])
        self.addCleanup(a.stop)
        self.addCleanup(b.stop)
        path = tempfile.mktemp(suffix=".jsonl")
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = nw.main(["poll", "--node", f"a,pj,{a.url}", "--node", f"b,pj,{b.url}",
                            "--count", "2", "--interval", "0", "--samples", path])
        self.assertEqual(code, 0, buf.getvalue())
        self.assertIn("#1 best 4 (1 head @4", buf.getvalue())
        self.assertIn("CHAIN VERDICT       : PASS", buf.getvalue())
        self.assertEqual(len(nw.load_samples(path)), 2)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(nw.main(["verdict", path, "--require-finality"]), 0,
                             "finality not yet advanced, but a 0-s run is not judged for it")

    def test_no_nodes_is_a_usage_error(self):
        with unittest.mock.patch.dict(os.environ, {}, clear=True), \
                redirect_stdout(io.StringIO()), unittest.mock.patch("sys.stderr", io.StringIO()):
            with self.assertRaises(SystemExit):
                nw.main(["poll"])

    def test_gate3a_is_a_thin_wrapper(self):
        import gate3a_soak
        with unittest.mock.patch.object(nw, "main", return_value=0) as m:
            self.assertEqual(gate3a_soak.main(["--node", "a,b,ws://x"]), 0)
        args = m.call_args[0][0]
        self.assertEqual(args[:5], ["poll", "--interval", "300", "--count", "160"])
        self.assertEqual(args[-2:], ["--node", "a,b,ws://x"])


class SoakVerdictChainSection(unittest.TestCase):
    """soak_verdict.py: unchanged without --chain/--parity; with them, the exit code is
    orders AND chain."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="jamswap_test_chain_")
        t = time.time() - 5000
        self.events = self.write("events.jsonl", "".join(json.dumps(e) + "\n" for e in [
            {"ts": t, "event": "placed", "market": 1, "account": 1, "oid": 1, "marketable": True},
            {"ts": t + 60, "event": "terminal", "market": 1, "account": 1, "oid": 1,
             "marketable": True, "outcome": "filled", "filled": 1, "latency": 60}]))
        ok = [healthy(1000.0 + 6 * i, 100 + i) for i in range(10)]
        bad = [dict(s, head={"slot": s["head"]["slot"], "blocks": dict(s["head"]["blocks"], c={"slot": 1, "hash": "x"})})
               for s in ok]
        self.good_chain = self.write("good.jsonl", "".join(json.dumps(s) + "\n" for s in ok))
        self.bad_chain = self.write("bad.jsonl", "".join(json.dumps(s) + "\n" for s in bad))
        self.good_parity = self.write("gp.json", json.dumps(
            {"pass": True, "reason": "all digests agree", "service": 1, "keys": 3, "attempt": 1,
             "at": {"mode": "final", "slot": 9, "hash": "ab" * 32, "from": "a"},
             "nodes": {"a": {"client": "x", "kind": "jip2", "pinned": True, "at": "ab", "digest": "d",
                             "present": 1, "error": None}}, "groups": {"d": ["a"]}, "mismatched_keys": {}}))
        self.bad_parity = self.write("bp.json", json.dumps({"pass": False, "reason": "2 different digests"}))

    def write(self, name, text):
        p = os.path.join(self.d, name)
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def run_cli(self, *extra, as_json=True):
        p = subprocess.run([sys.executable, VERDICT, self.events, *(["--json"] if as_json else []), *extra],
                           capture_output=True, text=True, timeout=60)
        return p.returncode, (json.loads(p.stdout) if as_json and p.stdout.strip() else p.stdout + p.stderr)

    def test_without_a_chain_the_report_is_unchanged(self):
        code, r = self.run_cli()
        self.assertEqual(code, 0)
        orders = soak_verdict.score(soak_verdict.load(self.events))
        self.assertEqual(set(r), set(orders), "no chain keys added")
        self.assertNotIn("chain", r)

    def test_a_good_chain_passes_and_a_bad_one_fails_the_soak(self):
        code, r = self.run_cli("--chain", self.good_chain, "--require-finality")
        self.assertEqual((code, r["pass"], r["orders_pass"], r["chain"]["pass"]), (0, True, True, True))
        self.assertTrue(r["chain"]["verdict"]["one_head"]["pass"])
        code, r = self.run_cli("--chain", self.bad_chain)
        self.assertEqual((code, r["pass"], r["orders_pass"]), (1, False, True))
        self.assertFalse(r["chain"]["verdict"]["one_head"]["pass"])

    def test_parity_joins_the_exit_code(self):
        self.assertEqual(self.run_cli("--parity", self.good_parity)[0], 0)
        code, r = self.run_cli("--chain", self.good_chain, "--parity", self.bad_parity)
        self.assertEqual((code, r["chain"]["verdict"]["pass"], r["chain"]["parity"]["pass"]), (1, True, False))

    def test_thresholds_reach_the_chain_verdict(self):
        code, r = self.run_cli("--chain", self.good_chain, "--epoch-slots", "50", "--max-lag", "7")
        self.assertEqual((r["chain"]["verdict"]["epoch_slots"], r["chain"]["verdict"]["one_head"]["max_lag_param"]),
                         (50, 7))

    def test_human_output_and_a_missing_chain_file(self):
        code, out = self.run_cli("--chain", self.good_chain, "--parity", self.good_parity, as_json=False)
        self.assertEqual(code, 0, out)
        for line in ("clearing SLO", "one head            : PASS", "finality            : PASS",
                     "state parity        : PASS", "VERDICT (orders + chain): PASS"):
            self.assertIn(line, out)
        code, out = self.run_cli("--chain", os.path.join(self.d, "nope.jsonl"), as_json=False)
        self.assertEqual(code, 2, out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
