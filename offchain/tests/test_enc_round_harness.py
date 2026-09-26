"""The encrypt-until-batch e2e's own logic (offchain/test_enc_round.py), without a chain.

The e2e claims a round was REJECTED only when it knows the round was processed, and claims
each attack trips only its own defence. Both rest on logic that can be checked here:

  * the scenario check: each attack round is the honest round with only its own fault
    (the tampered round once flipped a byte of the public section instead of a proof);
  * the jip2 sentinel: the gov-signed ENC_SETUP at the next nonce, signed by the key the
    service trusts (GOV_PUBKEY, read from service/src/lib.rs);
  * the work-report decoder behind the refine-layer check (GP 0.8.0 serialization), on a
    synthetic report and on one a PolkaJam 0.1.29 node served over JIP-2 `workReport` for a
    package we submitted;
  * each driver's processed signal, on scripted chains: the jip2 driver submits [round,
    sentinel] in one package and waits for the nonce at the finalized block, resubmitting a
    Failed package; the jamnp driver waits for the node's landed count and idleness and
    refuses error digests, dropped or abandoned items;
  * the layer check and the honest-round settlement check.

Run with:  python3 -m unittest discover -s offchain/tests
"""
import os
import re
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import chain                                                      # noqa: E402
import jip2                                                       # noqa: E402
import test_enc_round as e2e                                      # noqa: E402
from workpackage import enc_nat, var                              # noqa: E402

try:
    from nacl.signing import VerifyKey
    HAVE_NACL = True
except ImportError:                                               # pragma: no cover
    HAVE_NACL = False

SERVICE_SRC = os.path.join(os.path.dirname(__file__), "..", "..", "service", "src", "lib.rs")


def setUpModule():
    e2e.log = lambda msg: None                    # the drivers narrate to stderr




def fill(n, seed):
    return bytes((seed * 31 + i * 7) % 256 for i in range(n))


def enc_round(pks, cts, partials_seed=20, section=b"\0" * 4):
    """An ENC_ROUND with the layout refine_enc_round parses (the bytes need not verify)."""
    out = bytes([11]) + struct.pack("<III", 1, 10, 20) + bytes([len(pks) // 32]) + pks
    out += struct.pack("<H", len(cts))
    for ct in cts:
        out += ct[:32] + bytes([len(ct) - 32]) + ct[32:]
    return out + fill(e2e.PARTIAL_LEN * (len(pks) // 32) * len(cts), partials_seed) + section


def z_offset(honest, section_len=4):
    """The second-lowest byte of the last partial's response z."""
    return len(honest) - section_len - e2e.SCALAR_LEN + 1


def flipped(payload, at):
    b = bytearray(payload)
    b[at] ^= 0x01
    return bytes(b)


def scenario_lines(**override):
    """A structurally faithful `committee scenario 0` (the cryptography is filler)."""
    pks, evil = fill(64, 1), fill(64, 2)
    cts = [fill(49, 10), fill(49, 11)]

    def commit(ct, account):
        return (bytes([10]) + struct.pack("<I", 1) + ct + struct.pack("<IQ", account, 1)
                + bytes(64) + fill(32, 3))
    honest = enc_round(pks, cts)
    p = {
        "setup": bytes([9, 2]) + pks + struct.pack("<Q", 0) + bytes(64),
        "register_buy": bytes([7]) + fill(32, 4) + bytes(64),
        "register_sell": bytes([7]) + fill(32, 5) + bytes(64),
        "commit_buy": commit(cts[0], 1),
        "commit_sell": commit(cts[1], 2),
        "round": honest,
        "round_tampered": flipped(honest, z_offset(honest)),
        "round_wrongcommittee": enc_round(evil, [fill(49, 12), fill(49, 13)]),
        "round_injected": enc_round(pks, cts + [fill(49, 14)]),
    }
    p.update(override)
    return "\n".join(f"{k} {v.hex()}" for k, v in p.items()) + "\n"


class ScenarioCheck(unittest.TestCase):
    def test_a_faithful_scenario_is_accepted(self):
        sc = e2e.Scenario(scenario_lines())
        self.assertEqual(sc.committee, bytes([2]) + fill(64, 1))
        self.assertEqual(sc.entries, sorted([e2e.commitment(fill(49, 10)) + struct.pack("<I", 1),
                                             e2e.commitment(fill(49, 11)) + struct.pack("<I", 2)]))
        r = e2e.parse_round(sc.payloads["round_injected"])
        self.assertEqual(len(r["cts"]), 3)
        self.assertEqual(r["section"], b"\0" * 4)

    def test_the_old_tamper_of_the_public_section_is_refused(self):
        honest = enc_round(fill(64, 1), [fill(49, 10), fill(49, 11)])
        with self.assertRaisesRegex(ValueError, "round_tampered"):
            e2e.Scenario(scenario_lines(round_tampered=flipped(honest, len(honest) - 1)))

    def test_a_tamper_outside_a_proof_response_is_refused(self):
        honest = enc_round(fill(64, 1), [fill(49, 10), fill(49, 11)])
        share = z_offset(honest) - 2 * e2e.SCALAR_LEN          # the partial's S_i, not z
        for bad in (flipped(honest, share), flipped(flipped(honest, z_offset(honest)), 20),
                    honest):
            with self.assertRaisesRegex(ValueError, "round_tampered"):
                e2e.Scenario(scenario_lines(round_tampered=bad))

    def test_each_attack_must_carry_its_own_fault(self):
        honest = enc_round(fill(64, 1), [fill(49, 10), fill(49, 11)])
        with self.assertRaisesRegex(ValueError, "round_wrongcommittee"):
            e2e.Scenario(scenario_lines(round_wrongcommittee=honest))
        with self.assertRaisesRegex(ValueError, "round_injected"):
            e2e.Scenario(scenario_lines(round_injected=honest))
        with self.assertRaisesRegex(ValueError, "round "):
            e2e.Scenario(scenario_lines(round=enc_round(fill(64, 1), [fill(49, 10)])))

    def test_a_missing_line_is_named(self):
        text = "\n".join(ln for ln in scenario_lines().splitlines() if not ln.startswith("round_injected"))
        with self.assertRaisesRegex(ValueError, "round_injected"):
            e2e.Scenario(text)


@unittest.skipUnless(HAVE_NACL, "PyNaCl signs the sentinel")
class Sentinel(unittest.TestCase):
    def test_the_sentinel_is_signed_by_the_services_governance_key(self):
        with open(SERVICE_SRC) as f:
            src = f.read()
        m = re.search(r"const GOV_PUBKEY: \[u8; 32\] = \[(.*?)\];", src, re.S)
        gov = bytes(int(x, 16) for x in re.findall(r"0x([0-9a-f]{2})", m.group(1)))
        sc = e2e.Scenario(scenario_lines())
        s = sc.setup_payload(7)
        n, pks = sc.committee[:1], sc.committee[1:]
        self.assertEqual(s[:2 + 64], bytes([9]) + sc.committee)
        self.assertEqual(s[66:74], struct.pack("<Q", 7))
        VerifyKey(gov).verify(b"jamswap:v1:committee" + n + pks + struct.pack("<Q", 7), s[74:])


# ---- work-reports ---------------------------------------------------------------------
def digest(service, result):
    ok = isinstance(result, bytes)
    return (struct.pack("<I", service) + bytes(32) + bytes(32) + struct.pack("<Q", 9_999_999)
            + (b"\0" + var(result) if ok else bytes([result]))
            + enc_nat(123_456) + enc_nat(0) + enc_nat(0) + enc_nat(0) + enc_nat(0))


def report(digests, prereqs=1, trace=b"tr", lookups=1):
    return (bytes(104) + bytes(168) + enc_nat(prereqs) + bytes(32 * prereqs) + enc_nat(1)
            + bytes(32) + enc_nat(4242) + var(trace) + enc_nat(lookups) + bytes(64 * lookups)
            + enc_nat(len(digests)) + b"".join(digests))


# A work-report PolkaJam 0.1.29 served (JIP-2 workReport, 2026-09-26) for a package we
# submitted to our own service 1: one REGISTER item, output [7][pubkey].
POLKAJAM_REPORT = bytes.fromhex(
    "1ddf3d74f80196366c746e804369f02fa58b6966c5510b1490e7b10ec58125646a01000011dbd120ff76df58"
    "932c2ff4f63b10b9f35143fa80440f3d31f06b8b217a66990600000000000000000000000000000000000000"
    "000000000000000000000000000000009298183da3abeb5e5b08d975e7c6b466a5d7a864a768b87de35008b4"
    "f73e9a1577268b00e86597d44e0c0346df458848cf8a56a10e3c758d8465736690563470a7937bf56f2aca97"
    "c9c106cbf75da2617ebbdfc7c125b7a52f92affcd4351f097cb6fac592f5c3e375e3975d041492f7489541b2"
    "5f7a1b22d68246fa14e89f418fe19a7775268b00bdb392c600c8a8b18d5fc68a5621e4190633f4690d9a2909"
    "05339090325e464f00002357426f2313559a271d6782dc00197b379f79cbe3c6a1e72f61f7b592c509f81600"
    "000101000000176c7886e12ff302c285c52d6c8cdd2921bd31015af46d6db5363a9920f980206097bd1ccaa9"
    "7116150e8b43be351223630898540f23fb5d455ecc36c0f28de17f969800000000000021074721b5b632272e"
    "65a68dda7ac25b4185f8b01916db185c14287db92e2b770faee0cc355200000000")


class WorkReports(unittest.TestCase):
    def test_results_of_a_synthetic_report(self):
        r = report([digest(7, b"abc"), digest(8, 1), digest(9, b"")])
        self.assertEqual(e2e.report_results(r), [(7, "ok", b"abc"), (8, "out of gas", None),
                                                 (9, "ok", b"")])
        with self.assertRaisesRegex(ValueError, "left over"):
            e2e.report_results(r + b"\0")

    def test_results_of_a_report_polkajam_served(self):
        [(service, kind, out)] = e2e.report_results(POLKAJAM_REPORT)
        self.assertEqual((service, kind, len(out), out[0]), (1, "ok", 33, 7))

    def test_the_report_hash_is_found_by_name(self):
        status = {"Ready": {"reported_in": {"header_hash": "AAA=", "slot": 1}, "core": 0,
                            "report_hash": "Q0Q=", "ready_in": {"header_hash": "BBB="}}}
        self.assertEqual(e2e.find_hash(status), "Q0Q=")
        self.assertIsNone(e2e.find_hash({"Reportable": {"remaining_blocks": 7}}))


class Prometheus(unittest.TestCase):
    def test_labelled_series_are_summed_and_comments_skipped(self):
        g = e2e.prom("# TYPE x counter\nx 1\ny{result=\"oog\"} 2\ny{result=\"panic\"} 3\nz NaNx\n")
        self.assertEqual(g, {"x": 1.0, "y": 5.0})


# ---- the drivers on scripted chains ---------------------------------------------------------
class Clock:
    def __init__(self):
        self.t = 0.0

    def sleep(self, s):
        self.t += s

    def clock(self):
        return self.t


class ScriptedJip2:
    """Just what Jip2Driver touches. `nonces` is the committee nonce the finalized block
    shows at each poll (the best block shows nonces[0] before the submit)."""
    def __init__(self, nonces, statuses=(), round_output=b""):
        self.nonces, self.statuses = list(nonces), list(statuses)
        self.submitted, self.polls = [], 0
        out = round_output
        rep = jip2.b64(report([digest(5, out), digest(5, b"\x09" + bytes(73))]))
        self.rpc = type("Rpc", (), {"call": lambda _, m, h: rep})()

    def parameter(self, name, default=None):
        return default

    def read(self, key, at):
        assert key == b"comnonce"
        if at == "best":
            return struct.pack("<Q", self.nonces[0])
        self.polls += 1
        return struct.pack("<Q", self.nonces[min(self.polls, len(self.nonces) - 1)])

    def finalized(self):
        self.last_final = chain.Block(100 + self.polls, bytes(32), None)
        return self.last_final

    def head(self):
        return chain.Block(102 + self.polls, bytes(32), None)

    def submit_items(self, payloads):
        self.submitted.append(list(payloads))
        return {"package_hash": "ab" * 32, "core": 0, "anchor": "cd" * 32, "anchor_slot": 99}

    def package_status(self, receipt, at="best"):
        if at == "best" and self.statuses:
            return self.statuses.pop(0)
        return {"Ready": {"report_hash": "Q0Q="}}


@unittest.skipUnless(HAVE_NACL, "PyNaCl signs the sentinel")
class Jip2Processed(unittest.TestCase):
    def driver(self, ch):
        c = Clock()
        return e2e.Jip2Driver(ch, e2e.Scenario(scenario_lines()), b"code", 60,
                              sleep=c.sleep, clock=c.clock)

    def test_round_and_sentinel_go_in_one_package_and_the_nonce_says_processed(self):
        ch = ScriptedJip2([1, 1, 1, 2])
        d = self.driver(ch)
        at, ev = d.process(b"ROUND")
        self.assertEqual(ch.submitted, [[b"ROUND", d.sc.setup_payload(1)]])
        self.assertEqual(ev["allowed"], {"committee nonce": 2})
        self.assertEqual(ev["output"], 0)
        self.assertIs(at, ch.last_final)            # the state is read where the nonce showed
        self.assertIn("round item Ok, 0 output bytes; sentinel item Ok, 74 output bytes", ev["refine"])

    def test_a_failed_package_is_sent_again(self):
        ch = ScriptedJip2([1, 1, 1, 1, 2], statuses=[{"Failed": "anchor gone"}])
        self.driver(ch).process(b"ROUND")
        self.assertEqual(len(ch.submitted), 2)

    def test_a_nonce_that_jumps_is_someone_else(self):
        with self.assertRaisesRegex(e2e.CaseFailed, "someone else"):
            self.driver(ScriptedJip2([1, 3])).process(b"ROUND")

    def test_never_processed_times_out(self):
        with self.assertRaisesRegex(e2e.CaseFailed, "not accumulated"):
            self.driver(ScriptedJip2([1])).process(b"ROUND")


class ScriptedJamnp:
    def __init__(self):
        self.submitted = []

    def submit(self, payload):
        self.submitted.append(payload)
        return {"accepted": True}


def gauges(landed, queue=0, watched=0, errors=0, abandoned=0):
    return {"lasair_ce133_accumulated_total": landed, "lasair_ce133_queue_depth": queue,
            "lasair_guarantor_watched": watched, "lasair_guarantor_held": 0,
            "lasair_guarantor_error_digests_total": errors, "lasair_ce133_abandoned_total": abandoned,
            "lasair_block_height": 50, "lasair_finalized_height": 48}


class JamnpProcessed(unittest.TestCase):
    def run_with(self, sequence):
        c, ch = Clock(), ScriptedJamnp()
        d = e2e.JamnpDriver(ch, None, "http://node/metrics", 60, sleep=c.sleep, clock=c.clock)
        seq = list(sequence)
        d.gauges = lambda: seq.pop(0) if len(seq) > 1 else seq[0]
        return d, ch

    def test_waits_for_idle_then_a_landing_then_idle(self):
        d, ch = self.run_with([gauges(4, queue=1), gauges(4), gauges(4, queue=1),
                               gauges(5, watched=1), gauges(5)])
        at, ev = d.process(b"ROUND")
        self.assertEqual((at, ch.submitted, ev["allowed"], ev["output"]), ("best", [b"ROUND"], {}, None))
        self.assertIn("4 -> 5", ev["signal"])

    def test_an_error_digest_is_not_a_rejection(self):
        d, _ = self.run_with([gauges(4), gauges(5, errors=1)])
        with self.assertRaisesRegex(e2e.CaseFailed, "error digest"):
            d.process(b"ROUND")

    def test_an_abandoned_item_is_not_a_rejection(self):
        d, _ = self.run_with([gauges(4), gauges(5, abandoned=1)])
        with self.assertRaisesRegex(e2e.CaseFailed, "abandoned"):
            d.process(b"ROUND")

    def test_never_landing_times_out(self):
        d, _ = self.run_with([gauges(4)])
        with self.assertRaisesRegex(e2e.CaseFailed, "not accumulated"):
            d.process(b"ROUND")


# ---- verdicts ------------------------------------------------------------------------------
class Verdicts(unittest.TestCase):
    def test_the_layer_must_match_the_refine_output(self):
        self.assertIn("0 bytes", e2e.check_layer("t", e2e.REFINE, 0))
        self.assertIn("219 bytes", e2e.check_layer("w", e2e.ACCUMULATE, 219))
        self.assertIn("not observable", e2e.check_layer("x", e2e.REFINE, None))
        with self.assertRaisesRegex(e2e.CaseFailed, "rejected in refine"):
            e2e.check_layer("t", e2e.REFINE, 219)
        with self.assertRaisesRegex(e2e.CaseFailed, "judged in accumulate"):
            e2e.check_layer("w", e2e.ACCUMULATE, 0)

    def committed(self):
        sc = e2e.Scenario(scenario_lines())
        st = {name: b"" for name in e2e.state_keys(sc)}
        u64 = lambda v: struct.pack("<Q", v)                     # noqa: E731
        st.update({"balance buyer asset 20": u64(e2e.FUND), "balance seller asset 10": u64(e2e.FUND),
                   "custody asset 10": u64(e2e.FUND), "custody asset 20": u64(e2e.FUND),
                   "encset": b"".join(sc.entries), "committee": sc.committee})
        return sc, st

    def test_the_committed_state_is_recognised(self):
        sc, st = self.committed()
        e2e.check_committed(st, sc)
        with self.assertRaisesRegex(e2e.CaseFailed, "encset 1 entries"):
            e2e.check_committed(dict(st, encset=sc.entries[0]), sc)

    def test_the_honest_settlement(self):
        _, before = self.committed()
        u64 = lambda v: struct.pack("<Q", v)                     # noqa: E731
        after = dict(before, **{
            "balance buyer asset 10": u64(e2e.FILL_BASE),
            "balance buyer asset 20": u64(e2e.FUND - e2e.FILL_QUOTE),
            "balance seller asset 10": u64(e2e.FUND - e2e.FILL_BASE),
            "balance seller asset 20": u64(e2e.FILL_QUOTE),
            "encset": b"", "landed round": u64(7), "last price": u64(e2e.PRICE),
            "volume": u64(e2e.FILL_BASE)})
        e2e.check_settled(before, after)
        with self.assertRaisesRegex(e2e.CaseFailed, "not marked landed"):
            e2e.check_settled(before, dict(after, **{"landed round": b""}))
        with self.assertRaisesRegex(e2e.CaseFailed, "an attack round is marked landed"):
            e2e.check_settled(before, dict(after, **{"landed round_injected": u64(7)}))
        self.assertEqual(e2e.diff(before, before), {})
        self.assertEqual(set(e2e.diff(before, after, allowed={"encset"})),
                         {"balance buyer asset 10", "balance buyer asset 20", "balance seller asset 10",
                          "balance seller asset 20", "landed round", "last price", "volume"})


if __name__ == "__main__":
    unittest.main()
