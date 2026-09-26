"""JIP-2 transport tests: the stdlib WebSocket client and JSON-RPC framing (offchain/jip2.py),
and the chain adapter's jip2 backend (chain.Jip2Chain) mapping JIP-2 onto the interface.

Everything runs against fake_jip2.FakeJip2Node, a local WebSocket server written
independently of the client. The live check against a stock PolkaJam node is manual (see
the chain-adapter commit message); these pin the framing and the mapping.

Run with:  python3 -m unittest discover -s offchain/tests
"""
import base64
import os
import struct
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chain                                    # noqa: E402
import jip2                                     # noqa: E402
from fake_jip2 import FakeJip2Node, RpcError    # noqa: E402

B64 = lambda b: base64.b64encode(b).decode()    # noqa: E731


def unmask(data, key):
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


class Framing(unittest.TestCase):
    def test_accept_key_matches_rfc6455_example(self):
        # RFC 6455 section 1.3's worked example
        self.assertEqual(jip2.accept_key("dGhlIHNhbXBsZSBub25jZQ=="), "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=")

    def test_length_encodings(self):
        for n, head in ((0, b"\x81\x00"), (125, b"\x81\x7d"), (126, b"\x81\x7e\x00\x7e"),
                        (65535, b"\x81\x7e\xff\xff"),
                        (65536, b"\x81\x7f" + struct.pack(">Q", 65536))):
            frame = jip2.encode_frame(jip2.OP_TEXT, b"a" * n)
            self.assertEqual(frame[:len(head)], head, n)
            self.assertEqual(len(frame), len(head) + n)

    def test_client_frames_are_masked(self):
        payload = bytes(range(256)) * 3
        frame = jip2.encode_frame(jip2.OP_TEXT, payload, mask=b"\x01\x02\x03\x04")
        self.assertEqual(frame[:2], b"\x81\xfe")               # mask bit set, 16-bit length
        self.assertEqual(struct.unpack(">H", frame[2:4])[0], len(payload))
        self.assertEqual(frame[4:8], b"\x01\x02\x03\x04")
        self.assertEqual(unmask(frame[8:], b"\x01\x02\x03\x04"), payload)
        self.assertEqual(jip2.encode_frame(jip2.OP_TEXT, b"", mask=b"abcd"), b"\x81\x80abcd")


class Client(unittest.TestCase):
    def setUp(self):
        self.node = FakeJip2Node({"echo": lambda *p: list(p), "bestBlock": lambda: {"slot": 7}})
        self.rpc = jip2.Jip2Client(self.node.url, timeout=5)

    def tearDown(self):
        self.rpc.close()
        self.node.stop()

    def test_request_is_jsonrpc2_positional_and_masked(self):
        self.assertEqual(self.rpc.call("echo", 1, "two", [3]), [1, "two", [3]])
        req = self.node.requests[-1]
        self.assertEqual(req["jsonrpc"], "2.0")
        self.assertEqual(req["method"], "echo")
        self.assertEqual(req["params"], [1, "two", [3]])
        self.assertIsInstance(req["id"], int)
        self.assertTrue(self.node.masked and all(self.node.masked))

    def test_no_params_is_an_empty_array(self):
        self.rpc.call("bestBlock")
        self.assertEqual(self.node.requests[-1]["params"], [])

    def test_answers_of_every_length_class(self):
        for n in (10, 200, 70000):                  # 7-bit, 16-bit and 64-bit lengths
            self.assertEqual(self.rpc.call("echo", "x" * n), ["x" * n])

    def test_fragmented_answer_after_a_ping(self):
        self.node.fragment, self.node.ping_first = 7, True
        self.assertEqual(self.rpc.call("echo", "fragmented answer"), ["fragmented answer"])
        self.node.ping_first = False
        self.rpc.call("echo", 0)        # the node reads frames in order: our pong came first
        self.assertEqual(self.node.pongs, [b"are-you-there"])   # the ping was answered in kind

    def test_notification_before_the_answer_is_skipped(self):
        self.node.notify_first = True
        self.assertEqual(self.rpc.call("echo", 5), [5])

    def test_error_object(self):
        def boom():
            raise RpcError(1, "Block unavailable", "AAAA")
        self.node.methods["serviceValue"] = boom
        with self.assertRaises(jip2.Jip2Error) as cm:
            self.rpc.call("serviceValue")
        self.assertEqual((cm.exception.code, cm.exception.message, cm.exception.data),
                         (1, "Block unavailable", "AAAA"))
        self.assertEqual(self.rpc.call("echo", 1), [1])        # the connection survives an error

    def test_one_connection_is_reused(self):
        for i in range(5):
            self.rpc.call("echo", i)
        self.assertEqual(self.node.connections, 1)

    def test_read_retried_once_on_a_fresh_connection(self):
        self.node.drop_once.add("bestBlock")
        self.assertEqual(self.rpc.call("bestBlock"), {"slot": 7})
        self.assertEqual([r["method"] for r in self.node.requests], ["bestBlock", "bestBlock"])
        self.assertEqual(self.node.connections, 2)

    def test_submission_is_never_retried(self):
        self.node.drop_on.add("submitWorkPackage")
        with self.assertRaises(OSError):
            self.rpc.submit_work_package(0, b"package", [b"x1"])
        self.assertEqual([r["method"] for r in self.node.requests], ["submitWorkPackage"])
        self.assertEqual(self.node.requests[0]["params"], [0, B64(b"package"), [B64(b"x1")]])
        self.assertEqual(self.rpc.call("echo", 1), [1])        # next call reconnects

    def test_close_frame_raises(self):
        self.node.close_on.add("bestBlock")
        with self.assertRaises(jip2.WebSocketError) as cm:
            self.rpc.call("bestBlock", retry=False)
        self.assertIn("1011", str(cm.exception))

    def test_concurrent_callers_each_get_their_own_answer(self):
        errors, out = [], {}

        def worker(t):
            try:
                out[t] = [self.rpc.call("echo", t, i)[1] for i in range(20)]
            except Exception as e:          # noqa: BLE001 — surfaced below
                errors.append(e)
        threads = [threading.Thread(target=worker, args=(t,)) for t in range(8)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()
        self.assertEqual(errors, [])
        self.assertEqual(out, {t: list(range(20)) for t in range(8)})


class Handshake(unittest.TestCase):
    def test_bad_accept_is_refused(self):
        node = FakeJip2Node({"echo": lambda *p: p})
        node.bad_accept = True
        try:
            with self.assertRaises(jip2.WebSocketError):
                jip2.Jip2Client(node.url, timeout=5).call("echo", retry=False)
        finally:
            node.stop()

    def test_non_101_is_refused(self):
        node = FakeJip2Node({})
        node.status = 403
        try:
            with self.assertRaises(jip2.WebSocketError) as cm:
                jip2.Jip2Client(node.url, timeout=5).call("echo", retry=False)
            self.assertIn("403", str(cm.exception))
        finally:
            node.stop()

    def test_not_a_websocket_url(self):
        with self.assertRaises(jip2.WebSocketError):
            jip2.WebSocket("http://localhost:19800")


# ---- the adapter's jip2 backend ---------------------------------------------
BEST, FINAL = bytes([0xBB]) * 32, bytes([0xFF]) * 32
SID = 1234


def record(code_hash=bytes(range(32)), balance=10**12, min_item=10, min_memo=11, octets=4242,
           gratis=5, items=17, created=100, last_acc=200, parent=0):
    # GP 0.8.0 C(255, s): E(0, code_hash, E8(5 fields), E4(4 fields)), built independently
    return struct.pack("<B32s5Q4I", 0, code_hash, balance, min_item, min_memo, octets, gratis,
                       items, created, last_acc, parent)


class FakeChainNode(FakeJip2Node):
    """Two blocks (best and final) of one service's storage, served over JIP-2."""
    def __init__(self):
        self.state = {BEST: {b"k": b"best-value"}, FINAL: {b"k": b"final-value"}}
        super().__init__({
            "bestBlock": lambda: {"header_hash": B64(BEST), "slot": 9116412},
            "finalizedBlock": lambda: {"header_hash": B64(FINAL), "slot": 9116410},
            "serviceValue": self._value,
            "serviceData": lambda h, s: B64(record()) if s == SID else None,
            "parameters": lambda: {"V1": {"core_count": 2, "slot_period_sec": 6}},
        })

    def _value(self, h, s, k):
        if s != SID:
            return None
        v = self.state.get(base64.b64decode(h), {}).get(base64.b64decode(k))
        return None if v is None else B64(v)


class Jip2Backend(unittest.TestCase):
    def setUp(self):
        self.node = FakeChainNode()
        self.c = chain.Jip2Chain(SID, self.node.url, timeout=5)

    def tearDown(self):
        self.c.rpc.close()
        self.node.stop()

    def test_heads_are_block_descriptors(self):
        self.assertEqual(self.c.head(), chain.Block(9116412, BEST, None))
        self.assertEqual(self.c.finalized(), chain.Block(9116410, FINAL, None))
        self.assertTrue(self.c.ready())

    def test_read_at_best_final_and_explicit(self):
        self.assertEqual(self.c.read(b"k"), b"best-value")
        self.assertEqual(self.node.requests[-1]["params"], [B64(BEST), SID, B64(b"k")])
        self.assertEqual(self.c.read(b"k", at="final"), b"final-value")
        self.assertEqual(self.c.read(b"k", at=FINAL), b"final-value")
        self.assertEqual(self.c.read(b"k", at=chain.Block(1, BEST, None)), b"best-value")
        with self.assertRaises(ValueError):
            self.c.read(b"k", at="latest")

    def test_absent_value_reads_empty(self):
        self.assertEqual(self.c.read(b"missing"), b"")

    def test_service_info_decodes_the_account_record(self):
        info = self.c.service_info()
        self.assertEqual(info, chain.ServiceInfo(bytes(range(32)), 10**12, 10, 11, 4242, 5,
                                                 17, 100, 200, 0))
        self.c.service_id = SID + 1
        with self.assertRaises(chain.ChainError):
            self.c.service_info()                    # null: no such service

    def test_service_record_must_be_89_bytes_version_0(self):
        with self.assertRaises(chain.ChainError):
            chain.ServiceInfo.decode(record()[:-1])
        with self.assertRaises(chain.ChainError):
            chain.ServiceInfo.decode(b"\x01" + record()[1:])

    def test_parameters_pass_through(self):
        self.assertEqual(self.c.parameters(), {"V1": {"core_count": 2, "slot_period_sec": 6}})

    def test_submit_is_not_built_yet(self):
        self.assertFalse(self.c.submits)
        with self.assertRaises(NotImplementedError) as cm:
            self.c.submit(b"\x07payload")
        self.assertIsInstance(cm.exception, chain.ChainUnsupported)
        self.assertIn("#11", str(cm.exception))
        self.assertFalse(any(r["method"].startswith("submit") for r in self.node.requests))

    def test_node_errors_and_bad_answers_become_chain_errors(self):
        def unavailable(*_):
            raise RpcError(1, "Block unavailable")
        self.node.methods["serviceValue"] = unavailable
        with self.assertRaises(chain.ChainError):
            self.c.read(b"k")
        self.node.methods["bestBlock"] = lambda: {"slot": 1}              # no header_hash
        with self.assertRaises(chain.ChainError):
            self.c.head()
        self.node.methods["bestBlock"] = lambda: {"header_hash": "!!", "slot": 1}
        with self.assertRaises(chain.ChainError):
            self.c.head()

    def test_unreachable_node(self):
        self.node.stop()
        c = chain.Jip2Chain(SID, "ws://127.0.0.1:9", timeout=2)          # nothing listens
        with self.assertRaises(chain.ChainError):
            c.head()
        self.assertFalse(c.ready())


if __name__ == "__main__":
    unittest.main()
