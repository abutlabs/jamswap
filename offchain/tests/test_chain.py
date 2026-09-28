"""Chain adapter tests (offchain/chain.py) and the DEX's routing through it.

  * Selection: CHAIN_BACKEND / CHAIN_RPC / the jamnp env vars -> the right backend.
  * jamnp is BYTE-FOR-BYTE what server.py did before the adapter: the pre-adapter code
    (copied below from 0a371c2 as a reference oracle) and the jamnp backend are run
    against the same fake bridge, and every HTTP request (method, path, content-type,
    body) and every result must match. This is what keeps the lasair6 soak unchanged.
  * server.py reaches the chain only through CHAIN: storage, submit (incl. ChainBusy
    backpressure and the settle ledger), finality and the footprint, exercised against a
    fake in-memory backend.

Run with:  python3 -m unittest discover -s offchain/tests
"""
import importlib.util
import json
import os
import sys
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))
import chain      # noqa: E402
import metrics    # noqa: E402
from chain import Block, ChainBusy, ChainUnsupported   # noqa: E402

SID = 100


# ---- a fake lasair bridge: builder /submit, reader /read + /healthz, node /metrics -----
class FakeBridge:
    def __init__(self):
        self.requests = []           # (method, path, content-type, body)
        self.store = {}              # key hex -> value hex
        self.accept = True
        self.head_hex = "ab" * 32
        self.best_hex = None         # set: /healthz carries best_hex (a proving reader, lasair#70)
        self.read_error = None       # set: /read answers like lasair-reader when it can't read
        self.metrics = ("# HELP lasair_block_height best block height\n"
                        "lasair_block_height 120\nlasair_finalized_height 118\n"
                        "lasair_slot 7000123\nlasair_finalized_slot 7000121\n"
                        "process_cpu_seconds_total 3.5\nlasair_peers{kind=\"val\"} 5\n")
        bridge = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, body, ctype="application/json"):
                data = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                n = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(n) if n else b""
                bridge.requests.append((self.command, self.path, self.headers.get("Content-Type"), body))
                return body

            def do_GET(self):
                self._record()
                u = urlsplit(self.path)
                if u.path == "/read" and bridge.read_error:
                    self._reply({"found": False, "error": bridge.read_error})
                elif u.path == "/read":
                    q = parse_qs(u.query, keep_blank_values=True)
                    v = bridge.store.get(q["key"][0], "")
                    self._reply({"found": bool(v), "service": int(q["service"][0]),
                                 "head_hex": bridge.head_hex, "value_hex": v})
                elif u.path == "/healthz":
                    h = {"status": "ok", "head_hex": bridge.head_hex}
                    if bridge.best_hex is not None:
                        h["best_hex"] = bridge.best_hex
                    self._reply(h)
                elif u.path == "/metrics":
                    self._reply(bridge.metrics.encode(), "text/plain")
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self._record())
                self._reply({"accepted": bridge.accept, "service_id": body["service_id"],
                             "targets": [{"node": "lm0", "ok": bridge.accept}]})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02},
                         daemon=True).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()


# ---- the pre-adapter code paths, verbatim from server.py @ 0a371c2 (reference oracle) ----
def legacy_reader_get(READER_URL, path):
    req = urllib.request.Request(READER_URL + path, method="GET")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def legacy_storage(READER_URL, SID, key):
    r = legacy_reader_get(READER_URL, f"/read?service={SID}&key={key.hex()}")
    return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""


def legacy_post_json(url, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(url, data=data,
        headers={"content-type": "application/json"}, method="POST")
    return json.loads(urllib.request.urlopen(req, timeout=30).read())


def legacy_submit(BUILDER_URL, SID, payload):
    r = legacy_post_json(BUILDER_URL + "/submit", {"service_id": SID, "payload_hex": payload.hex()})
    if r.get("accepted") is False:
        raise ChainBusy("refused")
    return r


def legacy_read_finality(NODE_METRICS_URL):
    if not NODE_METRICS_URL:
        return {"available": False}
    try:
        txt = urllib.request.urlopen(NODE_METRICS_URL, timeout=2).read().decode()
        g = {}
        for ln in txt.splitlines():
            if ln.startswith("lasair_") and " " in ln:
                k, _, val = ln.partition(" ")
                try: g[k] = float(val)
                except ValueError: pass
        fh, bh = int(g.get("lasair_finalized_height", 0)), int(g.get("lasair_block_height", 0))
        v = {"available": fh > 0, "finalized_height": fh, "block_height": bh,
             "finalized_slot": int(g.get("lasair_finalized_slot", 0)),
             "slot": int(g.get("lasair_slot", 0)), "lag": max(0, bh - fh)}
    except Exception:
        v = {"available": False}
    return v


def fresh_server(backend):
    # a private copy of server.py bound to `backend`, so no other test's monkeypatching
    # of the shared `server` module can leak in (or out)
    spec = importlib.util.spec_from_file_location("server_under_test", os.path.join(HERE, "..", "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.CHAIN = backend
    return mod


class Selection(unittest.TestCase):
    def test_default_is_jamnp_with_todays_env(self):
        c = chain.from_env({"SERVICE_ID": "100", "BUILDER_URL": "http://builder:19980/",
                            "READER_URL": "http://reader:19990", "NODE_METRICS_URL": " http://lm0:9615/metrics "})
        self.assertIsInstance(c, chain.JamnpChain)
        self.assertEqual((c.service_id, c.builder_url, c.reader_url, c.metrics_url),
                         (100, "http://builder:19980", "http://reader:19990", "http://lm0:9615/metrics"))
        self.assertTrue(c.submits)

    def test_jip2_with_default_and_explicit_rpc(self):
        c = chain.from_env({"CHAIN_BACKEND": "jip2", "SERVICE_ID": "7"})
        self.assertIsInstance(c, chain.Jip2Chain)
        self.assertEqual((c.url, c.service_id), ("ws://localhost:19800", 7))
        c = chain.from_env({"CHAIN_BACKEND": " JIP2 ", "CHAIN_RPC": "ws://pj:19801"})
        self.assertEqual((c.url, c.service_id), ("ws://pj:19801", None))

    def test_unknown_backend(self):
        with self.assertRaises(ValueError):
            chain.from_env({"CHAIN_BACKEND": "http"})

    def test_missing_service_id_or_bridge_is_explained(self):
        with self.assertRaisesRegex(chain.ChainError, "SERVICE_ID"):
            chain.JamnpChain(None, "http://b", "http://r").read(b"k")
        with self.assertRaisesRegex(chain.ChainError, "BUILDER_URL"):
            chain.JamnpChain(SID, "", "http://r").submit(b"\x01")
        with self.assertRaisesRegex(chain.ChainError, "READER_URL"):
            chain.JamnpChain(SID, "http://b", "").read(b"k")


class JamnpIsUnchanged(unittest.TestCase):
    def setUp(self):
        self.bridge = FakeBridge()
        self.c = chain.JamnpChain(SID, self.bridge.url, self.bridge.url, self.bridge.url + "/metrics")

    def tearDown(self):
        self.bridge.stop()

    def _both(self, legacy, new):
        # run the legacy call then the adapter call; return (legacy result, new result,
        # legacy requests, new requests)
        n0 = len(self.bridge.requests)
        a = legacy()
        n1 = len(self.bridge.requests)
        b = new()
        return a, b, self.bridge.requests[n0:n1], self.bridge.requests[n1:]

    def test_read_is_the_same_request_and_value(self):
        self.bridge.store["62010000000200000000"] = "40420f0000000000"
        for key in (bytes.fromhex("62010000000200000000"), b"book\x01\x00\x00\x00", b""):
            a, b, ra, rb = self._both(lambda: legacy_storage(self.bridge.url, SID, key),
                                      lambda: self.c.read(key))
            self.assertEqual(a, b)
            self.assertEqual(ra, rb)
            self.assertEqual(len(rb), 1)
        self.assertEqual(self.c.read(bytes.fromhex("62010000000200000000")), bytes.fromhex("40420f0000000000"))

    def test_a_reader_that_cannot_read_raises_instead_of_reading_empty(self):
        # lasair-reader answers {"found": false, "error": ...} when the node is unreachable
        # or it has no head yet; that is not an absent key (an empty book, zero balances, no
        # seq floors) — the read must fail closed
        self.bridge.store["00"] = "01"
        self.bridge.read_error = "node unreachable: no QUIC connection"
        with self.assertRaises(chain.ChainError):
            self.c.read(bytes.fromhex("00"))
        self.bridge.read_error = None
        self.assertEqual(self.c.read(bytes.fromhex("00")), b"\x01")
        self.assertEqual(self.c.read(bytes.fromhex("ff")), b"", "an absent key still reads empty")

    def test_submit_is_the_same_request_and_receipt(self):
        payload = bytes([12]) + bytes(range(200))
        a, b, ra, rb = self._both(lambda: legacy_submit(self.bridge.url, SID, payload),
                                  lambda: self.c.submit(payload))
        self.assertEqual(a, b)
        self.assertEqual(ra, rb)
        method, path, ctype, body = rb[0]
        self.assertEqual((method, path, ctype), ("POST", "/submit", "application/json"))
        self.assertEqual(body, json.dumps({"service_id": SID, "payload_hex": payload.hex()}).encode())

    def test_refusal_is_chain_busy(self):
        self.bridge.accept = False
        with self.assertRaisesRegex(ChainBusy, "all guarantors refused"):
            self.c.submit(b"\x07")
        self.assertEqual(len(self.bridge.requests), 1)            # sent once, never retried

    def test_finality_dict_is_byte_identical(self):
        srv = fresh_server(self.c)
        for metrics_text in (self.bridge.metrics,                         # finalizing
                             "lasair_block_height 5\nlasair_slot 9\n",      # no finality gauges
                             ""):                                           # nothing exported
            self.bridge.metrics = metrics_text
            self.c._scrape = (0.0, None)
            srv._fin_cache["v"] = None
            want = legacy_read_finality(self.bridge.url + "/metrics")
            self.assertEqual(json.dumps(srv._read_finality()), json.dumps(want))
        self.bridge.stop()                                                  # node unreachable
        self.c._scrape = (0.0, None)
        srv._fin_cache["v"] = None
        self.assertEqual(srv._read_finality(), legacy_read_finality(self.bridge.url + "/metrics"))

    def test_no_metrics_url_means_no_finality_and_no_request(self):
        c = chain.JamnpChain(SID, self.bridge.url, self.bridge.url, "")
        self.assertIsNone(c.head())
        self.assertIsNone(c.finalized())
        self.assertEqual(fresh_server(c)._read_finality(), legacy_read_finality(""))
        self.assertEqual(self.bridge.requests, [])

    def test_head_and_finalized_share_one_scrape(self):
        self.assertEqual(self.c.finalized(), Block(7000121, None, 118))
        self.assertEqual(self.c.head(), Block(7000123, None, 120))
        self.assertEqual([p for _m, p, _c, _b in self.bridge.requests], ["/metrics"])

    def test_ready_follows_the_reader_head(self):
        self.assertTrue(self.c.ready())
        self.bridge.head_hex = ""
        self.assertFalse(self.c.ready())
        self.bridge.stop()
        self.assertFalse(self.c.ready())

    def test_a_proving_reader_is_ready_once_it_can_prove_a_block(self):
        # lasair#70: reads are proven at the head's parent (best_hex); at genesis there is
        # none and every read is refused, so the dex must not set up markets yet
        self.bridge.best_hex = ""
        self.assertFalse(self.c.ready())
        self.bridge.best_hex = "cd" * 32
        self.assertTrue(self.c.ready())

    def test_what_the_bridges_cannot_do(self):
        for call in (lambda: self.c.read(b"k", at="final"), lambda: self.c.read(b"k", at=b"\0" * 32),
                     self.c.service_info, self.c.parameters):
            with self.assertRaises(ChainUnsupported):
                call()
        self.assertEqual(self.bridge.requests, [])

    def test_footprint_degrades_exactly_as_before(self):
        srv = fresh_server(self.c)
        self.assertEqual(srv.footprint_octets(), 0)                  # QUIC mode used to return 0
        self.assertEqual(srv.api_footprint({}), {"available": False})
        self.assertEqual(self.bridge.requests, [])


class FakeChain(chain.Chain):
    """An in-memory backend."""
    name, submits = "fake", True

    def __init__(self):
        super().__init__(SID)
        self.store, self.sent, self.reads = {}, [], []
        self.refuse = None                    # an exception submit() raises instead
        self.best, self.final = Block(50, b"\xbb" * 32, None), Block(47, b"\xff" * 32, None)
        self.info = None

    def read(self, key, at="best"):
        self.reads.append((key, at))
        return self.store.get(key, b"")

    def submit(self, payload):
        if self.refuse:
            raise self.refuse
        self.sent.append(payload)
        return {"accepted": True}

    def head(self):
        return self.best

    def finalized(self):
        return self.final

    def service_info(self, at="best"):
        if self.info is None:
            raise ChainUnsupported("fake: no account record")
        return self.info


class ServerRoutesThroughTheAdapter(unittest.TestCase):
    def setUp(self):
        self.c = FakeChain()
        self.srv = fresh_server(self.c)

    def _ledger_state(self, op):
        return next(e["state"] for e in metrics.pending_snapshot() if e["op"] == op)

    def test_reads(self):
        self.c.store[b"b" + (2).to_bytes(4, "little") + (9).to_bytes(4, "little")] = (1234).to_bytes(8, "little")
        self.assertEqual(self.srv.bal(2, 9), 1234)
        self.assertEqual(self.srv.bal(2, 10), 0)
        self.assertEqual(self.srv.storage(b"govnonce"), b"")
        self.assertEqual(self.c.reads[-1], (b"govnonce", "best"))

    def test_submit_relays_and_tracks(self):
        r = self.srv.submit(bytes([7]) + b"\x01" * 96, check=lambda: False, detail="d")
        self.assertEqual(r, {"accepted": True})
        self.assertEqual(self.c.sent, [bytes([7]) + b"\x01" * 96])
        self.assertEqual(self._ledger_state("register"), "pending")

    def test_busy_is_backpressure_with_the_op_named(self):
        self.c.refuse = ChainBusy("all guarantors refused (CE-133 queues full)")
        with self.assertRaises(ChainBusy) as cm:
            self.srv.submit(bytes([5]) + b"\0" * 20, check=lambda: False)
        self.assertEqual(str(cm.exception), "withdraw: all guarantors refused (CE-133 queues full)")
        self.assertIs(self.srv.ChainBusy, ChainBusy)       # handlers catch the adapter's class
        self.assertEqual(self._ledger_state("withdraw"), "refused")

    def test_a_backend_that_cannot_submit(self):
        self.c.refuse = ChainUnsupported("fake: no submission")
        with self.assertRaises(NotImplementedError):
            self.srv.submit(bytes([4]) + b"\0" * 20, check=lambda: False)
        self.assertEqual(self._ledger_state("cancel"), "refused")

    def test_unknown_outcome_stays_pending(self):
        self.c.refuse = TimeoutError("bridge timed out")
        with self.assertRaises(TimeoutError):
            self.srv.submit(bytes([3]) + b"\0" * 20, check=lambda: False)
        self.assertEqual(self._ledger_state("reveal"), "pending")

    def test_finality_by_slot_when_blocks_carry_no_height(self):
        # blocks with hashes (jip2) also report them
        self.assertEqual(self.srv._read_finality(),
                         {"available": True, "finalized_height": 47, "block_height": 50,
                          "finalized_slot": 47, "slot": 50, "lag": 3, "ordinal": "slot",
                          "finalized_hash": "ff" * 32, "head_hash": "bb" * 32})

    def test_finality_by_height_when_reported(self):
        self.c.best, self.c.final = Block(50, None, 20), Block(47, None, 18)
        self.assertEqual(self.srv._read_finality(),
                         {"available": True, "finalized_height": 18, "block_height": 20,
                          "finalized_slot": 47, "slot": 50, "lag": 2})

    def test_finality_unavailable(self):
        self.c.final = None
        self.assertEqual(self.srv._read_finality(), {"available": False})
        self.c.finalized = lambda: (_ for _ in ()).throw(chain.ChainError("down"))
        self.srv._fin_cache["v"] = None
        self.assertEqual(self.srv._read_finality(), {"available": False})

    def test_finality_is_cached(self):
        first = self.srv._read_finality()
        self.c.best = Block(99, b"\x01" * 32, None)
        self.assertEqual(self.srv._read_finality(), first)

    def test_footprint_from_the_account_record(self):
        self.c.info = chain.ServiceInfo(b"\0" * 32, 0, 0, 0, 5000, 0, 12, 0, 0, 0)
        self.assertEqual(self.srv.footprint_octets(), 5000)
        self.assertEqual(self.srv.api_footprint({}), {"items": 12, "octets": 5000, "available": True})
        self.assertEqual(self.srv.rent_reserve_atomic(), 5 * self.srv.SCALE)   # ceil(5000/1024) KB

    def test_ready(self):
        self.c.ready = lambda: True
        self.srv.wait_for_node()                           # returns at once


if __name__ == "__main__":
    unittest.main()
