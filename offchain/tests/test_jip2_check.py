"""offchain/jip2_check.py against a fake JIP-2 node: a node that serves everything, one
missing a required method, one missing only an optional one, and one without a best block.
"""
import io
import os
import sys
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.dirname(HERE))

import jip2_check                               # noqa: E402
from fake_jip2 import FakeJip2Node, RpcError    # noqa: E402

H = jip2_check.ZERO


def refuse(*_):
    raise RpcError(-32602, "Invalid params")


def full_node():
    return {
        "parameters": lambda: {"V": 6},
        "bestBlock": lambda: {"header_hash": H, "slot": 9},
        "finalizedBlock": lambda: {"header_hash": H, "slot": 8},
        "parent": lambda h: {"header_hash": H, "slot": 8},
        "stateRoot": lambda h: H,
        "listServices": lambda h: [0, 100],
        "serviceData": lambda h, s: H,
        "serviceValue": lambda h, s, k: None,
        "servicePreimage": lambda h, s, p: None,
        "serviceRequest": lambda h, s, p, n: None,
        "workPackageStatus": lambda h, p, a: None,
        "submitWorkPackage": refuse,
        "submitPreimage": refuse,
        "syncState": lambda: {"num_peers": 5, "status": "Completed"},
        "statistics": lambda h: H,
    }


def run(methods):
    node = FakeJip2Node(methods)
    out = io.StringIO()
    try:
        with redirect_stdout(out):
            code = jip2_check.main(["jip2_check.py", node.url])
    finally:
        node.stop()
    return code, out.getvalue(), node


class Jip2Check(unittest.TestCase):
    def test_a_full_node_can_run_jamswap(self):
        code, out, node = run(full_node())
        self.assertEqual(code, 0, out)
        self.assertIn("jamswap can run on this node", out)
        self.assertIn("present, runtime deploy possible", out)
        # the submissions are probed with no arguments: nothing can reach the chain
        subs = [r for r in node.requests if r["method"].startswith("submit")]
        self.assertEqual([r["params"] for r in subs], [[], []])

    def test_a_missing_required_method_fails(self):
        m = full_node()
        del m["servicePreimage"]
        code, out, _ = run(m)
        self.assertEqual(code, 1)
        self.assertIn("MISS  servicePreimage", out)
        self.assertIn("jamswap needs: servicePreimage", out)

    def test_a_missing_submission_fails(self):
        m = full_node()
        del m["submitWorkPackage"]
        code, out, _ = run(m)
        self.assertEqual(code, 1)
        self.assertIn("jamswap needs: submitWorkPackage", out)

    def test_optional_methods_do_not_fail(self):
        m = full_node()
        del m["syncState"], m["statistics"]
        code, out, _ = run(m)
        self.assertEqual(code, 0, out)
        self.assertIn("(optional)", out)

    def test_no_bootstrap_service_is_reported_not_failed(self):
        m = full_node()
        m["serviceData"] = lambda h, s: None if s == 0 else H
        code, out, _ = run(m)
        self.assertEqual(code, 0, out)
        self.assertIn("absent: seed the service into genesis", out)

    def test_no_best_block_fails_the_dependent_reads(self):
        m = full_node()
        del m["bestBlock"]
        code, out, _ = run(m)
        self.assertEqual(code, 1)
        self.assertIn("not probed: bestBlock gave no header_hash", out)

    def test_an_unreachable_node(self):
        out = io.StringIO()
        with redirect_stdout(out):
            code = jip2_check.main(["jip2_check.py", "ws://127.0.0.1:9"])
        self.assertEqual(code, 2)
        self.assertIn("cannot reach", out.getvalue())


if __name__ == "__main__":
    unittest.main()
