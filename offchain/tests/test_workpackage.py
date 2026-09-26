"""The GP 0.8.0 work-package codec (offchain/workpackage.py) and the JIP-4 chain-spec
authorizer lookup (offchain/chainspec.py).

Expected bytes are assembled here field by field with struct, from the GP 0.8.0 text
(serialization.tex; merklization.tex for the state keys), not with the module's helpers.
When jam-types-py 0.8.0 (an independent GP codec) is installed, the encoding is also
decoded and re-encoded by it: set JAM_TYPES_PYTHON to an interpreter that can import
jam_types (default ~/.venvs/jam-types-0.8.0/bin/python); the check skips without one.

Run with:  python3 -m unittest discover -s offchain/tests
"""
import hashlib
import json
import os
import struct
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chainspec                                      # noqa: E402
import workpackage as wp                              # noqa: E402

H = lambda *parts: hashlib.blake2b(b"".join(parts), digest_size=32).digest()   # noqa: E731


def h(n):
    return bytes([n]) * 32


CTX = wp.RefineContext(h(1), 9116722, h(2), h(3), h(4), 9116721, h(5))
ITEM = wp.WorkItem(7, h(8), b"\x07payload", 999_999_999, 9_999_999)
AUTH = wp.Authorizer(0, h(12))


class Naturals(unittest.TestCase):
    # E(x) per GP 0.8.0 eq. C.1, worked by hand: 2^7l <= x < 2^7(l+1) gives the prefix
    # 2^8 - 2^(8-l) + floor(x / 2^8l) and then x mod 2^8l in l little-endian octets
    VECTORS = [
        (0, "00"), (1, "01"), (127, "7f"),
        (128, "8080"), (16383, "bfff"),                       # l = 1
        (16384, "c00040"), (2 ** 21 - 1, "dfffff"),           # l = 2
        (2 ** 21, "e0000020"),                                # l = 3
        (2 ** 56 - 1, "fe" + "ff" * 7),                       # l = 7
        (2 ** 56, "ff" + "00" * 7 + "01"), (2 ** 64 - 1, "ff" + "ff" * 8),
    ]

    def test_vectors(self):
        for x, hexed in self.VECTORS:
            self.assertEqual(wp.enc_nat(x).hex(), hexed, x)
            self.assertEqual(wp.dec_nat(bytes.fromhex(hexed)), (x, len(hexed) // 2), x)

    def test_out_of_range(self):
        for x in (-1, 2 ** 64):
            with self.assertRaises(ValueError):
                wp.enc_nat(x)

    def test_decode_rejects_non_canonical_and_truncated(self):
        for bad in ("8001", "c00000", "ff0100000000000000", "80", "e00000", ""):
            with self.assertRaises(ValueError, msg=bad):
                wp.dec_nat(bytes.fromhex(bad))


class Encoding(unittest.TestCase):
    def test_refine_context_layout(self):
        want = (h(1) + struct.pack("<I", 9116722) + h(2) + h(3) + h(4)
                + struct.pack("<I", 9116721) + h(5) + b"\x00")
        self.assertEqual(CTX.encode(), want)
        self.assertEqual(len(want), 32 * 5 + 4 * 2 + 1)

    def test_prerequisites_are_a_sorted_set(self):
        ctx = CTX._replace(prerequisites=(h(9), h(6)))
        self.assertEqual(ctx.encode()[-65:], b"\x02" + h(6) + h(9))

    def test_work_item_layout(self):
        want = (struct.pack("<I", 7) + h(8) + struct.pack("<QQH", 999_999_999, 9_999_999, 0)
                + b"\x08" + b"\x07payload" + b"\x00" + b"\x00")
        self.assertEqual(ITEM.encode(), want)

    def test_imports_and_extrinsics(self):
        item = ITEM._replace(imports=(wp.ImportRef(h(9), 5), wp.ImportRef(h(10), 7, True)),
                             extrinsics=((h(11), 77),), export_count=3)
        want = (struct.pack("<I", 7) + h(8) + struct.pack("<QQH", 999_999_999, 9_999_999, 3)
                + b"\x08" + b"\x07payload"
                + b"\x02" + h(9) + struct.pack("<H", 5) + h(10) + struct.pack("<H", 7 | 0x8000)
                + b"\x01" + h(11) + struct.pack("<I", 77))
        self.assertEqual(item.encode(), want)
        with self.assertRaises(ValueError):
            wp.ImportRef(h(9), 1 << 15).encode()

    def test_package_layout_and_hash(self):
        auth = wp.Authorizer(3, h(12), config=b"cfg", token=b"tok")
        pkg = wp.WorkPackage.build(auth, CTX, [ITEM])
        want = (struct.pack("<I", 3) + h(12) + CTX.encode() + b"\x03tok" + b"\x03cfg"
                + b"\x01" + ITEM.encode())
        self.assertEqual(pkg.encode(), want)
        self.assertEqual(pkg.hash(), H(want))
        self.assertEqual(pkg.authorizer, H(h(12), b"cfg"))
        self.assertEqual(auth.hash, pkg.authorizer)

    def test_a_long_payload_gets_a_two_byte_length(self):
        item = ITEM._replace(payload=b"x" * 300)
        self.assertEqual(item.encode()[4 + 32 + 18:4 + 32 + 18 + 2], bytes.fromhex("812c"))

    def test_round_trip(self):
        item = ITEM._replace(imports=(wp.ImportRef(h(9), 5, True),), extrinsics=((h(11), 77),))
        pkg = wp.WorkPackage(1, h(12), CTX._replace(prerequisites=(h(6),)),
                             (item, ITEM._replace(service=5, payload=b"")), b"tok", b"cfg")
        self.assertEqual(wp.WorkPackage.decode(pkg.encode()), pkg)

    def test_decode_rejects_trailing_and_truncated(self):
        enc = wp.WorkPackage.build(AUTH, CTX, [ITEM]).encode()
        for bad in (enc + b"\x00", enc[:-1], enc[:40]):
            with self.assertRaises(ValueError):
                wp.WorkPackage.decode(bad)

    def test_bad_inputs(self):
        with self.assertRaises(ValueError):
            wp.WorkPackage.build(AUTH, CTX, []).encode()          # at least one item
        with self.assertRaises(ValueError):
            CTX._replace(anchor=b"short").encode()
        with self.assertRaises(ValueError):
            ITEM._replace(code_hash=h(1)[:31]).encode()


class Authorizers(unittest.TestCase):
    def test_parse_and_print(self):
        a = wp.Authorizer.parse("0:" + h(12).hex())
        self.assertEqual(a, wp.Authorizer(0, h(12)))
        self.assertEqual(str(a), "0:" + h(12).hex())
        b = wp.Authorizer.parse(f" 5:0x{h(12).hex()}:c0ff:ee ")
        self.assertEqual(b, wp.Authorizer(5, h(12), b"\xc0\xff", b"\xee"))
        self.assertEqual(wp.Authorizer.parse(str(b)), b)
        self.assertEqual(a.hash, H(h(12)))

    def test_parse_errors(self):
        for bad in ("", "0", "0:abcd", "x:" + h(1).hex(), "0:" + h(1).hex() + ":zz", "0:1:2:3:4"):
            with self.assertRaises(ValueError, msg=bad):
                wp.Authorizer.parse(bad)


def jam_types_python():
    exe = os.environ.get("JAM_TYPES_PYTHON") or os.path.expanduser("~/.venvs/jam-types-0.8.0/bin/python")
    if not os.path.exists(exe):
        return None
    ok = subprocess.run([exe, "-c", "import jam_types"], capture_output=True).returncode == 0
    return exe if ok else None


JAM_TYPES_ROUNDTRIP = r"""
import json, sys
from jam_types import WorkPackage, ScaleBytes
data = bytes.fromhex(sys.stdin.read())
obj = WorkPackage(data=ScaleBytes(data))
value = obj.decode()
print(json.dumps({"value": value, "reencoded": WorkPackage().encode(value).data.hex()}))
"""


class CrossCheckWithJamTypes(unittest.TestCase):
    def test_jam_types_decodes_and_reencodes_identically(self):
        exe = jam_types_python()
        if exe is None:
            self.skipTest("jam-types-py 0.8.0 not installed (set JAM_TYPES_PYTHON)")
        item = ITEM._replace(imports=(wp.ImportRef(h(9), 5), wp.ImportRef(h(10), 7, True)),
                             extrinsics=((h(11), 77),), export_count=3, payload=b"\x0c" * 300)
        pkg = wp.WorkPackage(1, h(12), CTX._replace(prerequisites=(h(6), h(7))),
                             (item, ITEM), b"tok", b"cfg")
        enc = pkg.encode()
        out = subprocess.run([exe, "-c", JAM_TYPES_ROUNDTRIP], input=enc.hex(),
                             capture_output=True, text=True, check=True)
        got = json.loads(out.stdout)
        self.assertEqual(got["reencoded"], enc.hex())
        v = got["value"]
        self.assertEqual((v["auth_code_host"], v["auth_code_hash"]), (1, "0x" + h(12).hex()))
        self.assertEqual(v["authorization"], "0x" + b"tok".hex())
        self.assertEqual(v["authorizer_config"], "0x" + b"cfg".hex())
        c = v["context"]
        self.assertEqual((c["anchor"], c["anchor_slot"], c["state_root"], c["beefy_root"]),
                         ("0x" + h(1).hex(), 9116722, "0x" + h(2).hex(), "0x" + h(3).hex()))
        self.assertEqual((c["lookup_anchor"], c["lookup_anchor_slot"], c["lookup_anchor_state_root"]),
                         ("0x" + h(4).hex(), 9116721, "0x" + h(5).hex()))
        self.assertEqual(c["prerequisites"], ["0x" + h(6).hex(), "0x" + h(7).hex()])
        w = v["items"][0]
        self.assertEqual((w["service"], w["code_hash"], w["refine_gas_limit"],
                          w["accumulate_gas_limit"], w["export_count"]),
                         (7, "0x" + h(8).hex(), 999_999_999, 9_999_999, 3))
        self.assertEqual(w["payload"], "0x" + ("0c" * 300))
        self.assertEqual([(s["tree_root"], s["index"]) for s in w["import_segments"]],
                         [("0x" + h(9).hex(), 5), ("0x" + h(10).hex(), 7 | 0x8000)])
        self.assertEqual([(x["hash"], x["len"]) for x in w["extrinsic"]], [("0x" + h(11).hex(), 77)])


# ---- JIP-4 chain spec -> authorizer --------------------------------------------
def c_key(s, blob):
    # C(s, blob) per GP 0.8.0 merklization.tex, written out independently
    n, a = struct.pack("<I", s), H(blob)
    return bytes([n[0], a[0], n[1], a[1], n[2], a[2], n[3], a[3]]) + a[4:27]


def account_key(s):
    n = struct.pack("<I", s)
    return bytes([255, n[0], 0, n[1], 0, n[2], 0, n[3]]) + bytes(23)


def spec_with(pools, preimages, services=(0,), prefix=""):
    """A JIP-4 spec whose genesis has these pools, these (service, blob) preimages, an
    account record per service, a storage item and a preimage request (noise)."""
    state = {bytes([1]) + bytes(30): b"".join(wp.enc_nat(len(p)) + b"".join(p) for p in pools)}
    for s in services:
        state[account_key(s)] = bytes(89)
        state[c_key(s, struct.pack("<I", 2 ** 32 - 1) + b"storage-key")] = b"a stored value"
    for s, blob in preimages:
        state[c_key(s, struct.pack("<I", 2 ** 32 - 2) + H(blob))] = blob
        state[c_key(s, struct.pack("<I", len(blob)) + H(blob))] = b"\x01" + bytes(4)
    return {"id": "test", "genesis_header": "00",
            "genesis_state": {prefix + k.hex(): prefix + v.hex() for k, v in state.items()}}


class ChainSpecAuthorizer(unittest.TestCase):
    NULL_AUTH = b"\x05\x00not really PVM code, just bytes"
    OTHER = b"another preimage"

    def test_finds_the_empty_config_authorizer_and_its_cores(self):
        a = H(H(self.NULL_AUTH))
        spec = spec_with([[h(99), a], [a, a]], [(5, self.NULL_AUTH), (0, self.OTHER)],
                         services=(0, 5))
        found = chainspec.authorizers(spec)
        self.assertEqual(found, [(wp.Authorizer(5, H(self.NULL_AUTH)), [0, 1])])
        self.assertEqual(chainspec.authorizer(spec), found[0])

    def test_only_the_cores_whose_pool_holds_it(self):
        a = H(H(self.NULL_AUTH))
        spec = spec_with([[h(99)], [a], []], [(0, self.NULL_AUTH)], prefix="0x")
        self.assertEqual(chainspec.authorizer(spec), (wp.Authorizer(0, H(self.NULL_AUTH)), [1]))

    def test_the_one_on_most_cores_wins(self):
        a, b = H(H(self.NULL_AUTH)), H(H(self.OTHER))
        spec = spec_with([[a], [b], [b]], [(0, self.NULL_AUTH), (0, self.OTHER)])
        self.assertEqual(chainspec.authorizer(spec), (wp.Authorizer(0, H(self.OTHER)), [1, 2]))

    def test_a_value_that_is_not_under_its_preimage_key_does_not_count(self):
        # the pool names H(H(blob)), but the blob is stored as a plain storage item
        a = H(H(self.NULL_AUTH))
        spec = spec_with([[a]], [])
        spec["genesis_state"][c_key(0, struct.pack("<I", 2 ** 32 - 1) + b"k").hex()] = self.NULL_AUTH.hex()
        self.assertEqual(chainspec.authorizers(spec), [])
        with self.assertRaisesRegex(ValueError, "AUTHORIZER"):
            chainspec.authorizer(spec)

    def test_state_key_matches_the_gp_layout(self):
        blob = struct.pack("<I", 2 ** 32 - 2) + h(3)
        self.assertEqual(chainspec.state_key(0x04030201, blob), c_key(0x04030201, blob))
        self.assertEqual(chainspec.state_key(0x04030201, blob)[0:8:2], bytes([1, 2, 3, 4]))

    def test_malformed_specs(self):
        with self.assertRaisesRegex(ValueError, "C\\(1\\)"):
            chainspec.authorizers({"genesis_state": {}})
        with self.assertRaisesRegex(ValueError, "31 bytes"):
            chainspec.genesis_state({"genesis_state": {"0100": "00"}})
        with self.assertRaisesRegex(ValueError, "truncated"):
            chainspec.auth_pools({bytes([1]) + bytes(30): b"\x02" + h(1)})


if __name__ == "__main__":
    unittest.main()
