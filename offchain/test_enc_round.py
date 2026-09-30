#!/usr/bin/env python3
"""E2E: encrypt-until-batch rounds on a live chain, through the chain adapter (jamswap#24).

The builder attacks on an encrypted round, each against a FULL committed set (both sealed
orders committed on chain) so it can trip only its own defence, then the honest round:

  case                  what the builder did                        rejected by
  round_tampered        flipped a byte of a Chaum-Pedersen proof     refine: the proof fails,
                                                                     so the output is empty
  round_wrongcommittee  decrypted with its own committee (keys,      accumulate: the round's
                        partials and ciphertexts)                    committee hash is not the
                                                                     on-chain committee's
  round_injected        added a ciphertext nobody committed          accumulate: consume-or-reject
  round                 the committee's real decryption              settles: buyer +5 base,
                                                                     seller +500 quote, encset
                                                                     consumed

The payloads come from `committee scenario 0` (crates/committee): market 1 trading asset
10 against asset 20, the buyer as handle 1 and the seller as handle 2.

A rejected round must leave the service state byte-identical: balances, custody, the
encset, book, market stats, committee, order and commit floors, carry credits, the landed
marker of every round (and, where the backend reads the account record, its storage item
and octet counts). "Rejected" is only claimed once the round is known to have been
PROCESSED, i.e. accumulated whatever accumulate did with it, never because it has not
landed yet:

  jip2   the round goes in one work-package with a sentinel after it: the gov-signed
         ENC_SETUP of the same committee at the next nonce, whose only effect is
         b"comnonce" += 1 (same keys, same sizes). A package's items reach the service in
         one accumulate call, in order (GP 0.8.0 section 12), so the nonce moving at the
         FINALIZED block means the round was accumulated there; the state is read at that
         same block and must equal the state before, the nonce aside.
  jamnp  lasair's bridges take one payload per package, so the round goes alone and the
         signal is the guarantor node's own count of landed work-items (Prometheus at
         NODE_METRICS_URL: lasair_ce133_accumulated_total + lasair_ce133_landed_errors_total,
         counted when an item's report is accumulated on its best chain). The node is
         idle before the submit (nothing queued, refining, watched or held), so +1 is this
         round; the state is read once the node is idle again (its landing block final).
         An error digest (a refine that ran out of gas or panicked) fails the case: that
         would reject the round for the wrong reason.

A fresh service per case:

  jip2   each case deploys its own service through the chain's Bootstrap service
         (deploy.py) and sets it up, as the retired RPC-era test did.
  jamnp  lasair has no Bootstrap service yet (lasair#73), so SERVICE_ID must be a service
         seeded EMPTY at genesis (`lasair_client --service <jam> --service-id <id>`): it is
         set up once, then takes the four cases in turn. That is the same test: each attack
         is asserted to leave the state byte-identical, so the next case starts from
         exactly the committed state a fresh service would have, and the honest round runs
         last, so it settles only if the committed set survived all three attacks.

## Run it

    # payloads: the committee binary (cargo build --release in crates/committee), or a
    # file holding its `scenario 0` output (ENC_SCENARIO) where the binary cannot run
    export COMMITTEE=crates/committee/target/release/committee

    # PolkaJam (or any JIP-2 node with the Bootstrap service): a service per case
    CHAIN_BACKEND=jip2 CHAIN_RPC=ws://localhost:19800 CHAIN_SPEC=dev-spec.json \\
        python3 offchain/test_enc_round.py

    # lasair: the CE-133 builder and CE-129 reader bridges and the node's metrics
    CHAIN_BACKEND=jamnp BUILDER_URL=http://127.0.0.1:19980 READER_URL=http://127.0.0.1:19800 \\
        NODE_METRICS_URL=http://127.0.0.1:9615/metrics SERVICE_ID=100 \\
        python3 offchain/test_enc_round.py

`--json PATH` writes the evidence (every key before and after, per case). Exit 0 when every
assertion holds, 1 when one fails; if no chain is configured it SKIPS (exit 0).
"""
import argparse, json, os, struct, subprocess, sys, time, urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import chain as chainmod                                       # noqa: E402
import deploy                                                  # noqa: E402
import dex_setup                                               # noqa: E402
from server import canon, commitment, gov_sign, round_id       # noqa: E402  (one definition each)
from workpackage import dec_nat                                # noqa: E402

MARKET, BASE, QUOTE = 1, 10, 20            # committee scenario 0
BUYER, SELLER = 1, 2                       # the handles its orders and commits name
FEE_ACCOUNT = 0xFFFF_FFFF                  # FEE_ACCOUNT in the service
SCALE = 10_000
FUND = 10_000 * SCALE                      # buyer's quote, seller's base
FILL_BASE = 5 * SCALE                      # both orders: 5 base at 100 quote
FILL_QUOTE = 5 * 100 * SCALE
PRICE = 100 * SCALE
DEPOSIT_NONCE = 1
POINT_LEN, ORDER_LEN = 32, 17              # vdec::POINT_LEN, wire::ORDER_LEN
SCALAR_LEN, PARTIAL_LEN = 32, 96           # vdec: a partial is S_i(32) ‖ e(32) ‖ z(32)
ENTRY_LEN = 36                             # encset entry: H(C1 ‖ body) ‖ account
TAG_ENC_SETUP = 9

REFINE, ACCUMULATE = "refine", "accumulate"
# (payload name, what the builder did, the layer that must reject it and why)
ATTACKS = [
    ("round_tampered", "tampered Chaum-Pedersen proof", REFINE, "a proof fails: empty output"),
    ("round_wrongcommittee", "wrong committee keys", ACCUMULATE, "committee hash mismatch"),
    ("round_injected", "injected uncommitted ciphertext", ACCUMULATE, "consume-or-reject"),
]
HONEST = ("round", "honest committee decryption", None, "settles")
CASES = ATTACKS + [HONEST]
SCENARIO_LINES = ("setup", "register_buy", "register_sell", "commit_buy", "commit_sell",
                  *(c[0] for c in CASES))
DEFAULT_COMMITTEE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crates",
                                 "committee", "target", "release", "committee")


class CaseFailed(AssertionError):
    pass


def p32(x):
    return struct.pack("<I", x)


def p64(x):
    return struct.pack("<Q", x)


def le(v):
    return int.from_bytes(v, "little") if v else 0


def log(msg):
    print(msg, file=sys.stderr, flush=True)


# ---- the scenario ---------------------------------------------------------------------
class Scenario:
    """`committee scenario 0`'s payloads and what they imply on chain."""

    def __init__(self, text):
        p = {}
        for line in text.splitlines():
            parts = line.split()
            if len(parts) == 2:
                p[parts[0]] = bytes.fromhex(parts[1])
        missing = [k for k in SCENARIO_LINES if k not in p]
        if missing:
            raise ValueError(f"committee scenario: no {', '.join(missing)} line")
        self.payloads = p
        setup = p["setup"]                       # [9][n][pks n*32][nonce 8][sig 64]
        self.committee = setup[1:2 + 32 * setup[1]]
        self.buyer_pk, self.seller_pk = p["register_buy"][1:33], p["register_sell"][1:33]
        self.entries = sorted([commit_entry(p["commit_buy"]), commit_entry(p["commit_sell"])])
        self.check_attacks()

    def check_attacks(self):
        """Each attack round must be the honest round with only its own fault, or a case can
        pass for the wrong reason (round_tampered once flipped a byte of the public section,
        so refine rejected a malformed section and never checked a proof)."""
        r = {c[0]: parse_round(self.payloads[c[0]]) for c in CASES}
        honest, ids = r["round"], {e[:32] for e in self.entries}

        def need(ok, name, why):
            if not ok:
                raise ValueError(f"committee scenario: {name} {why}")
        need(honest["committee"] == self.committee and {commitment(c) for c in honest["cts"]} == ids,
             "round", "is not the committed committee decrypting the two committed ciphertexts")
        for name, x in r.items():
            need(x["header"] == honest["header"] and x["section"] == honest["section"], name,
                 "names another market or carries another public section")
        h, t = self.payloads["round"], self.payloads["round_tampered"]
        flips = [i for i in range(min(len(h), len(t))) if h[i] != t[i]]
        start, end = honest["partials"]
        need(len(h) == len(t) and len(flips) == 1 and start <= flips[0] < end
             and (flips[0] - start) % PARTIAL_LEN >= POINT_LEN + SCALAR_LEN,
             "round_tampered", f"is not the honest round with one byte of a proof response z "
                               f"flipped (differs at {flips[:4]}, partials at {start}..{end})")
        w = r["round_wrongcommittee"]
        need(w["committee"] != self.committee and len(w["committee"]) == len(self.committee),
             "round_wrongcommittee", "does not carry another committee of the same size")
        inj = r["round_injected"]
        extra = [c for c in inj["cts"] if commitment(c) not in ids]
        need(inj["committee"] == self.committee and inj["cts"][:len(honest["cts"])] == honest["cts"]
             and len(extra) == 1 and len(inj["cts"]) == len(honest["cts"]) + 1,
             "round_injected", "is not the honest round plus one uncommitted ciphertext")

    @classmethod
    def load(cls, committee=None, path=None):
        if path:
            with open(path) as f:
                return cls(f.read())
        try:
            out = subprocess.run([committee, "scenario", "0"], capture_output=True, text=True,
                                 check=True, timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise SystemExit(f"committee scenario: {e} (build crates/committee, or pass "
                             "--scenario / ENC_SCENARIO)") from None
        return cls(out.stdout)

    def setup_payload(self, nonce):
        """ENC_SETUP of this committee at `nonce`, signed by the (public, demo) governance
        key: the service's canon(committee, n, pks, nonce). At nonce 0 it is the
        committee binary's own `setup` line, byte for byte (ed25519 is deterministic)."""
        n, pks = self.committee[:1], self.committee[1:]
        return (bytes([TAG_ENC_SETUP]) + n + pks + p64(nonce)
                + gov_sign(canon(b"committee", n, pks, p64(nonce))))

    def funding(self):
        return [(BUYER, QUOTE, dex_setup.deposit_payload(BUYER, QUOTE, FUND, DEPOSIT_NONCE)),
                (SELLER, BASE, dex_setup.deposit_payload(SELLER, BASE, FUND, DEPOSIT_NONCE))]


def commit_entry(commit):
    """The encset entry an ENC_COMMIT adds: H(C1 ‖ body) ‖ account.
    [10][market 4][C1 32][body 17][account 4][seq 8][sig 64][pk 32]"""
    ct_end = 5 + POINT_LEN + ORDER_LEN
    return commitment(commit[5:ct_end]) + commit[ct_end:ct_end + 4]


def parse_round(p):
    """An ENC_ROUND payload: [11][market][base][quote][k][pks k*32][m:u16], m x ([C1 32]
    [len:u8][body]), m*k partials, then the public section (service refine_enc_round)."""
    k = p[13]
    i = 14 + 32 * k
    m = le(p[i:i + 2])
    i += 2
    cts = []
    for _ in range(m):
        n = p[i + POINT_LEN]
        cts.append(p[i:i + POINT_LEN] + p[i + POINT_LEN + 1:i + POINT_LEN + 1 + n])
        i += POINT_LEN + 1 + n
    end = i + m * k * PARTIAL_LEN
    return {"header": p[:13], "committee": p[13:14 + 32 * k], "cts": cts,
            "partials": (i, end), "section": p[end:]}


def entries_of(encset):
    return sorted(encset[i:i + ENTRY_LEN] for i in range(0, len(encset) - ENTRY_LEN + 1, ENTRY_LEN))


# ---- the state a round may touch ---------------------------------------------------------
def state_keys(sc):
    """name -> storage key: everything an encrypted round reads or writes on this market."""
    k = {}
    for asset in (BASE, QUOTE):
        for who, acct in (("buyer", BUYER), ("seller", SELLER), ("fees", FEE_ACCOUNT)):
            k[f"balance {who} asset {asset}"] = b"b" + p32(asset) + p32(acct)
        k[f"custody asset {asset}"] = b"cust" + p32(asset)
    k["market"] = b"mkt" + p32(MARKET)
    k["encset"] = b"encset" + p32(MARKET)
    k["book"] = b"book" + p32(MARKET)
    k["last price"] = b"lp" + p32(MARKET)
    k["volume"] = b"cv" + p32(MARKET)
    k["committee"] = b"committee"
    k["committee nonce"] = b"comnonce"
    k["next handle"] = b"nexthandle"
    for who, acct in (("buyer", BUYER), ("seller", SELLER)):
        k[f"order floor {who}"] = b"sq" + p32(acct)
        k[f"commit floor {who}"] = b"sc" + p32(acct)
        k[f"carry credits {who}"] = b"cw" + p32(MARKET) + p32(acct)
    for c in CASES:
        k[f"landed {c[0]}"] = b"rl" + round_id(sc.payloads[c[0]])
    return k


def describe(st):
    """One line per concern, for the evidence printout."""
    b = lambda who, a: le(st[f"balance {who} asset {a}"])   # noqa: E731
    ents = entries_of(st["encset"])
    landed = [c[0] for c in CASES if st[f"landed {c[0]}"]]
    rec = st.get("record")
    return [
        f"buyer  base {b('buyer', BASE):>11} quote {b('buyer', QUOTE):>11} | "
        f"seller base {b('seller', BASE):>11} quote {b('seller', QUOTE):>11} | "
        f"fees {b('fees', BASE)}/{b('fees', QUOTE)} | custody {le(st[f'custody asset {BASE}'])}"
        f"/{le(st[f'custody asset {QUOTE}'])}",
        f"encset {len(ents)} entries [" + ", ".join(f"{e[:4].hex()}..:{le(e[32:])}" for e in ents)
        + f"] | book {len(st['book'])} B | last price {le(st['last price'])} | volume "
        f"{le(st['volume'])} | committee {commitment(st['committee'])[:4].hex()}.. nonce "
        f"{le(st['committee nonce'])} | landed rounds: {', '.join(landed) or 'none'}"
        + (f" | record items {rec['items']} octets {rec['octets']}" if rec else ""),
    ]


def diff(before, after, allowed=()):
    """{name: (before, after)} for every value that changed, except those in `allowed`."""
    return {k: (before.get(k), after.get(k)) for k in sorted(set(before) | set(after))
            if k not in allowed and before.get(k) != after.get(k)}


def show(v):
    return v.hex() if isinstance(v, (bytes, bytearray)) else v


# ---- work-report results (jip2 only, best effort) -----------------------------------------
RESULT_ERRORS = {1: "out of gas", 2: "panic", 3: "bad exports", 4: "oversize", 5: "BAD code",
                 6: "code too BIG"}


def report_results(report):
    """[(service, 'ok', output) | (service, error, None)] of an encoded GP 0.8.0 work-report
    (serialization.tex): the availability spec (104 octets), the refinement context
    (168 + var prerequisites), core, authorizer hash, auth gas, var trace, var segment-root
    lookup, then var digests: E4 service, code hash, payload hash, E8 gas limit, O(result),
    then five naturals."""
    i = 104 + 168
    n, i = dec_nat(report, i)
    i += 32 * n                                   # prerequisites
    _, i = dec_nat(report, i)                     # core
    i += 32                                       # authorizer hash
    _, i = dec_nat(report, i)                     # auth gas used
    n, i = dec_nat(report, i)
    i += n                                        # auth trace
    n, i = dec_nat(report, i)
    i += 64 * n                                   # segment-root lookup
    n, i = dec_nat(report, i)
    out = []
    for _ in range(n):
        service = le(report[i:i + 4])
        i += 4 + 32 + 32 + 8
        kind = report[i]
        i += 1
        if kind == 0:
            m, i = dec_nat(report, i)
            out.append((service, "ok", bytes(report[i:i + m])))
            i += m
        else:
            out.append((service, RESULT_ERRORS.get(kind, f"error {kind}"), None))
        for _ in range(5):
            _, i = dec_nat(report, i)
    if i != len(report):
        raise ValueError(f"work-report: {len(report) - i} octets left over")
    return out


def find_hash(obj, keys=("report_hash", "work_report_hash", "report")):
    """A base64 report hash anywhere in a JIP-2 status object (its members are not
    pinned by the JIP, so look for them by name)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k in keys and isinstance(v, str):
                return v
            h = find_hash(v, keys)
            if h:
                return h
    return None


# ---- backends: how a round gets processed and the state read back ---------------------
class Driver:
    """What differs per backend: where each case's service comes from, how a submitted
    round is known to have been processed, and where the state is read. `sleep` and
    `clock` are injectable for tests."""
    fresh_per_case = False

    def __init__(self, ch, sc, timeout, sleep=time.sleep, clock=time.monotonic):
        self.ch, self.sc, self.timeout = ch, sc, timeout
        self.sleep, self.clock = sleep, clock

    def prepare(self):
        """A service for the next case: its id if it needs the setup, else None."""
        raise NotImplementedError

    def record(self, at):
        """The account record's footprint at `at`, or None where it cannot be read."""
        return None

    def settled_point(self):
        """Where snapshots read by default."""
        return "best"

    def process(self, payload):
        """Submit the round; return once it was accumulated: (where to read the state
        after it, {"signal": how that is known, "allowed": {key name: the value it must now
        have}, "refine": what is known of its refine, "output": the round's refine output
        length, or None where the backend cannot see it})."""
        raise NotImplementedError


class Jip2Driver(Driver):
    """A service per case through the Bootstrap service; the round and the nonce sentinel
    in one work-package; reads at the finalized block where the sentinel shows."""
    fresh_per_case = True
    POLL = 3.0

    def __init__(self, ch, sc, code, timeout, **kw):
        super().__init__(ch, sc, timeout, **kw)
        self.code = code
        self.recent = self.ch.parameter("recent_block_count", 8)

    def prepare(self):
        return deploy.deploy(self.ch, self.code, fresh=True, log=log, timeout=self.timeout).service_id

    def record(self, at):
        info = self.ch.service_info(at)
        return {"items": info.items, "octets": info.octets, "balance": info.balance,
                "code_hash": info.code_hash.hex()}

    def settled_point(self):
        return self.ch.finalized()

    def process(self, payload):
        n0 = le(self.ch.read(b"comnonce", "best"))
        sentinel = self.sc.setup_payload(n0)
        deadline = self.clock() + self.timeout
        receipt = None
        while self.clock() < deadline:
            if receipt is None:
                try:
                    receipt = self.ch.submit_items([payload, sentinel])
                    log(f"   submitted [round, sentinel ENC_SETUP nonce {n0}] as package "
                        f"{receipt['package_hash'][:16]}.. on core {receipt['core']}, anchor "
                        f"#{receipt['anchor_slot']}")
                except chainmod.ChainBusy as e:
                    log(f"   not sent ({e}); retrying")
            self.sleep(self.POLL)
            try:
                fin = self.ch.finalized()
                n = le(self.ch.read(b"comnonce", fin))
                if n == n0 + 1:
                    text, output = self.refine_evidence(receipt, fin)
                    return fin, {"signal": f"committee nonce {n0} -> {n} at finalized "
                                           f"#{fin.slot} (the sentinel after the round in its "
                                           f"package)", "allowed": {"committee nonce": n},
                                 "refine": text, "output": output}
                if n > n0 + 1:
                    raise CaseFailed(f"committee nonce moved {n0} -> {n}: someone else "
                                     "is using this service")
                if receipt is not None and self.lost(receipt):
                    receipt = None                # nothing of it was accumulated: send again
            except chainmod.ChainError as e:
                log(f"   waiting: {e}")
        raise CaseFailed(f"the round was not accumulated within {self.timeout:g}s")

    def lost(self, receipt):
        """Can the package never be accumulated? JIP-2 reports it Failed, or (as deploy.py
        judges it) it has no status and its anchor has left recent history."""
        try:
            status = self.ch.package_status(receipt)
        except chainmod.ChainError as e:
            status = {"Unknown": str(e)}
        kind = next(iter(status)) if isinstance(status, dict) and status else None
        aged = self.ch.head().slot > receipt["anchor_slot"] + self.recent + deploy.READY_GRACE_SLOTS
        if kind == "Failed" or (kind in (None, "Unknown") and aged):
            log(f"   package {receipt['package_hash'][:16]}.. {status}; resubmitting")
            return True
        return False

    def refine_evidence(self, receipt, at):
        """(text, the round's refine output length or None): from the package's work-report
        where the node serves one (JIP-2 workPackageStatus names it, workReport returns it).
        An error result (out of gas, panic ...) fails the case: the round was not refined."""
        try:
            h = find_hash(self.ch.package_status(receipt, at))
            if not h:
                return "n/a (workPackageStatus names no report)", None
            results = report_results(chainmod.jip2.unb64(self.ch.rpc.call("workReport", h)))
        except (chainmod.ChainError, chainmod.jip2.Jip2Error, OSError, ValueError) as e:
            return f"n/a ({e})", None
        text = f"work-report {chainmod.jip2.unb64(h)[:8].hex()}..: " + "; ".join(
            f"{what} item " + (f"Ok, {len(out)} output bytes" if kind == "ok" else kind)
            for what, (_, kind, out) in zip(("round", "sentinel"), results))
        if len(results) != 2 or any(kind != "ok" for _, kind, _ in results):
            raise CaseFailed(f"the package did not refine as [round, sentinel]: {text}")
        return text, len(results[0][2])


def prom(text):
    """{metric name: value} of a Prometheus text page, labelled series summed per name."""
    g = {}
    for ln in text.splitlines():
        if not ln or ln[0] == "#":
            continue
        name, _, val = ln.rpartition(" ")
        name = name.split("{", 1)[0]
        try:
            g[name] = g.get(name, 0.0) + float(val)
        except ValueError:
            pass
    return g


class JamnpDriver(Driver):
    """One genesis-seeded service for every case; a round alone per package; the node's
    landed-item count as the processed signal."""
    POLL = 1.0
    IDLE = ("lasair_ce133_queue_depth", "lasair_guarantor_watched", "lasair_guarantor_held")
    LOST = ("lasair_ce133_abandoned_total", "lasair_ce133_dropped_total")
    ERRORS = ("lasair_guarantor_error_digests_total", "lasair_ce133_landed_errors_total")

    def __init__(self, ch, sc, metrics_url, timeout, **kw):
        super().__init__(ch, sc, timeout, **kw)
        if not metrics_url:
            raise SystemExit("jamnp: set NODE_METRICS_URL (the guarantor node's /metrics): "
                             "its landed-item count tells when a round was processed")
        self.url, self.prepared = metrics_url, False

    def gauges(self):
        return prom(urllib.request.urlopen(self.url, timeout=5).read().decode())

    @staticmethod
    def landed(g):
        return (g.get("lasair_ce133_accumulated_total", 0) + g.get("lasair_ce133_landed_errors_total", 0)
                - g.get("lasair_ce133_reorged_total", 0))

    def idle(self, g):
        return all(g.get(k, 0) == 0 for k in self.IDLE)

    @staticmethod
    def total(g, names):
        return sum(g.get(k, 0) for k in names)

    def prepare(self):
        if self.prepared:
            return None
        self.prepared = True
        if self.ch.read(b"rl" + round_id(self.sc.payloads["round"])):
            raise SystemExit(f"service {self.ch.service_id} already settled this scenario's "
                             "honest round: run on a service seeded empty at genesis")
        return self.ch.service_id

    def process(self, payload):
        deadline = self.clock() + self.timeout
        g0 = self.gauges()
        while not self.idle(g0):                  # nothing of ours may be in flight
            if self.clock() >= deadline:
                raise CaseFailed("the node never went idle before the submit")
            self.sleep(self.POLL)
            g0 = self.gauges()
        l0 = self.landed(g0)
        while True:
            try:
                self.ch.submit(payload)
                break
            except chainmod.ChainBusy as e:
                if self.clock() >= deadline:
                    raise CaseFailed(f"never accepted: {e}") from None
                log(f"   not accepted ({e}); retrying")
                self.sleep(self.POLL)
        log(f"   submitted the round alone ({len(payload)} B); node had {l0:g} landed items")
        while self.clock() < deadline:
            self.sleep(self.POLL)
            g = self.gauges()
            if not (self.landed(g) >= l0 + 1 and self.idle(g)):
                continue
            # idle again: everything the node accepted has landed final, been dropped as
            # another copy's duplicate, or been abandoned; only a landing is our round's
            if self.total(g, self.LOST) != self.total(g0, self.LOST):
                raise CaseFailed("the node dropped or abandoned a work-item: cannot tell the "
                                 "round was accumulated")
            if self.total(g, self.ERRORS) != self.total(g0, self.ERRORS):
                raise CaseFailed("the round landed as an error digest (refine ran out of gas "
                                 "or panicked): that is not the service rejecting it")
            extra = self.landed(g) - l0 - 1
            self.sleep(1.5)                       # the reader follows the head the node announces
            return "best", {
                "signal": f"node's landed items {l0:g} -> {self.landed(g):g}, idle at height "
                          f"{g.get('lasair_block_height', 0):g} (finalized "
                          f"{g.get('lasair_finalized_height', 0):g}), no error digest"
                          + (f", {extra:g} more than this round" if extra else ""),
                "allowed": {}, "output": None,
                "refine": "not visible to the bridges (the node logs it: [guarantor] refined .. output bytes)"}
        raise CaseFailed(f"the round was not accumulated within {self.timeout:g}s")


# ---- setup ---------------------------------------------------------------------------------
def setup(ch, sc, timeout):
    """Register the buyer then the seller (handles 1, 2), list market 1, fund both, commit the
    committee and both sealed orders; each op resubmitted until it shows (the service
    treats a duplicate of any of them as a no-op)."""
    runner = dex_setup.Runner(ch, log, poll=2.0, resend=90.0)
    p = sc.payloads
    handle = lambda pk: dex_setup.handle_of(ch, pk)          # noqa: E731
    regs = [dex_setup.Op("register buyer", p["register_buy"], lambda: handle(sc.buyer_pk) is not None),
            dex_setup.Op("register seller", p["register_sell"], lambda: handle(sc.seller_pk) is not None)]
    t0 = time.monotonic()
    left = lambda: max(1.0, timeout - (time.monotonic() - t0))   # noqa: E731
    if ch.max_items >= 2:                         # one package: accumulated in this order
        runner.run("registrations", regs, left())
    else:
        for op in regs:
            runner.run(op.what, [op], left())
    got = (handle(sc.buyer_pk), handle(sc.seller_pk))
    if got != (BUYER, SELLER):
        raise CaseFailed(f"buyer/seller registered as handles {got}, not (1, 2): the scenario's "
                         "orders name handles 1 and 2, so the service must be empty to start")
    market = ch.read(b"mkt" + p32(MARKET))
    if market and market[:8] != p32(BASE) + p32(QUOTE):
        raise CaseFailed(f"market {MARKET} is already listed as {market.hex()}, not {BASE}/{QUOTE}")
    encset = lambda: entries_of(ch.read(b"encset" + p32(MARKET)))   # noqa: E731
    ops = [dex_setup.Op(f"list market {MARKET}", dex_setup.list_payload(MARKET, BASE, QUOTE),
                        lambda: ch.read(b"mkt" + p32(MARKET)) == p32(BASE) + p32(QUOTE))]
    ops += [dex_setup.Op(f"fund {acct} with asset {asset}", pl,
                         lambda acct=acct: dex_setup.deposit_landed(ch, acct, DEPOSIT_NONCE))
            for acct, asset, pl in sc.funding()]
    ops += [dex_setup.Op("commit the committee", p["setup"],
                         lambda: ch.read(b"committee") == sc.committee)]
    ops += [dex_setup.Op(f"sealed commit {who}", p[f"commit_{who}"],
                         lambda e=commit_entry(p[f"commit_{who}"]): e in encset())
            for who in ("buy", "sell")]
    runner.run("market, funds, committee, sealed commits", ops, left())


def check_committed(st, sc):
    """The state every case starts from: both sealed orders committed, nothing settled."""
    want = {"balance buyer asset 20": FUND, "balance seller asset 10": FUND,
            "balance buyer asset 10": 0, "balance seller asset 20": 0}
    bad = {k: le(st[k]) for k, v in want.items() if le(st[k]) != v}
    if bad or entries_of(st["encset"]) != sc.entries or st["committee"] != sc.committee:
        raise CaseFailed(f"not the committed starting state: balances {bad or 'ok'}, encset "
                         f"{len(entries_of(st['encset']))} entries (want both commits), committee "
                         f"{'ok' if st['committee'] == sc.committee else st['committee'].hex()}")


# ---- the cases -----------------------------------------------------------------------------
def snapshot(ch, keys, driver, at=None):
    """Every key (and the account record, where readable) at one point: by default the
    finalized block on jip2 (every read at that one block), the reader's head on jamnp."""
    at = driver.settled_point() if at is None else at
    st = {name: ch.read(key, at) for name, key in keys.items()}
    rec = driver.record(at)
    if rec is not None:
        st["record"] = rec
    return st


def committed_snapshot(ch, keys, driver, sc):
    """The starting state, once the setup shows where the snapshot reads (on jip2 the
    finalized block, which trails the best block the setup watched)."""
    deadline = driver.clock() + driver.timeout
    while True:
        try:
            st = snapshot(ch, keys, driver)
            check_committed(st, sc)
            return st
        except (CaseFailed, chainmod.ChainError):
            if driver.clock() >= deadline:
                raise
        driver.sleep(3)


def check_layer(name, layer, output):
    """Where the backend shows the round's refine output, it must match the layer that is
    to reject it: empty when refine rejects, a round output when accumulate must."""
    if output is None:
        return "layer not observable on this backend"
    if (layer == REFINE) != (output == 0):
        raise CaseFailed(f"{name}: refine output is {output} bytes, but the round should be "
                         + ("rejected in refine" if layer == REFINE else "refined, and judged in accumulate"))
    return f"observed: refine output {output} bytes"


def run_case(ch, sc, keys, driver, case):
    name, what, layer, why = case
    sid = driver.prepare()
    if sid is not None:
        setup(ch, sc, driver.timeout)
    log(f"== {name}: {what} (service {ch.service_id})")
    before = committed_snapshot(ch, keys, driver, sc)
    for line in describe(before):
        log(f"   before  {line}")
    at, ev = driver.process(sc.payloads[name])
    after = snapshot(ch, keys, driver, at)
    changed = diff(before, after, allowed=ev["allowed"])
    for k, v in ev["allowed"].items():            # the sentinel did exactly its one thing
        if le(after[k]) != v:
            raise CaseFailed(f"{name}: {k} is {le(after[k])}, want {v}")
    log(f"   signal  {ev['signal']}")
    log(f"   refine  {ev['refine']}")
    for line in describe(after):
        log(f"   after   {line}")
    result = {"case": name, "attack": what, "service": ch.service_id, "signal": ev["signal"],
              "refine": ev["refine"], "before": {k: show(v) for k, v in before.items()},
              "after": {k: show(v) for k, v in after.items()}}
    if layer is not None:
        if changed:
            raise CaseFailed(f"{name}: expected rejection, but the state changed: "
                             + json.dumps({k: [show(a), show(b)] for k, (a, b) in changed.items()}))
        seen = check_layer(name, layer, ev["output"])
        result["verdict"] = (f"REJECTED in {layer} ({why}; {seen}), state unchanged: "
                             f"{len(keys)} keys" + (" + account record" if "record" in after else ""))
    else:
        check_layer(name, ACCUMULATE, ev["output"])
        check_settled(before, after)
        result["verdict"] = (f"SETTLED: buyer +{FILL_BASE} base, seller +{FILL_QUOTE} quote, "
                             "encset consumed, round marked landed")
    log(f"   verdict {result['verdict']}")
    return result


def check_settled(before, after):
    b = lambda st, who, a: le(st[f"balance {who} asset {a}"])   # noqa: E731
    want = {("buyer", BASE): FILL_BASE, ("buyer", QUOTE): FUND - FILL_QUOTE,
            ("seller", BASE): FUND - FILL_BASE, ("seller", QUOTE): FILL_QUOTE,
            ("fees", BASE): b(before, "fees", BASE), ("fees", QUOTE): b(before, "fees", QUOTE)}
    bad = {f"{w} {a}": b(after, w, a) for (w, a), v in want.items() if b(after, w, a) != v}
    problems = []
    if bad:
        problems.append(f"balances {bad}")
    for a in (BASE, QUOTE):                       # a trade conserves custody
        if after[f"custody asset {a}"] != before[f"custody asset {a}"]:
            problems.append(f"custody of asset {a} changed")
    if entries_of(after["encset"]):
        problems.append(f"encset still holds {len(entries_of(after['encset']))} entries")
    if not after["landed round"]:
        problems.append("the round is not marked landed")
    if le(after["last price"]) != PRICE or le(after["volume"]) != le(before["volume"]) + FILL_BASE:
        problems.append(f"last price {le(after['last price'])} / volume {le(after['volume'])}")
    if after["book"] != before["book"] or after["committee"] != before["committee"]:
        problems.append("the book or the committee changed")
    if any(after[k] for k in after if k.startswith("landed round_")):
        problems.append("an attack round is marked landed")
    if problems:
        raise CaseFailed("honest round: " + "; ".join(problems))


def main(argv=None):
    env = os.environ
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--backend", default=env.get("CHAIN_BACKEND") or "jamnp", choices=chainmod.BACKENDS)
    ap.add_argument("--committee", default=env.get("COMMITTEE") or DEFAULT_COMMITTEE,
                    help="the committee binary (COMMITTEE)")
    ap.add_argument("--scenario", default=env.get("ENC_SCENARIO"),
                    help="a file with `committee scenario 0` output, instead of running it")
    ap.add_argument("--code", default=env.get("SERVICE_CODE") or deploy.DEFAULT_CODE,
                    help="the service blob a jip2 case deploys (SERVICE_CODE)")
    ap.add_argument("--timeout", type=float, default=float(env.get("CASE_TIMEOUT") or 600),
                    help="seconds per deploy, setup and round")
    ap.add_argument("--only", help="comma-separated cases to run (default: all, honest last)")
    ap.add_argument("--json", help="write the evidence here")
    a = ap.parse_args(argv)

    ch = chainmod.from_env(dict(env, CHAIN_BACKEND=a.backend))
    need = ("CHAIN_RPC",) if a.backend == "jip2" else ("BUILDER_URL", "READER_URL")
    if not all(env.get(v) for v in need):
        print(f"SKIP: no chain configured for {a.backend} (set {', '.join(need)}); see --help")
        return 0
    sc = Scenario.load(a.committee, a.scenario)
    if sc.setup_payload(0) != sc.payloads["setup"]:
        raise SystemExit("the governance key here does not sign the committee binary's ENC_SETUP")
    if a.backend == "jip2":
        if not ch.submits:
            raise SystemExit("jip2: set CHAIN_SPEC or AUTHORIZER (the authorizer to submit with)")
        with open(a.code, "rb") as f:
            driver = Jip2Driver(ch, sc, f.read(), a.timeout)
    else:
        if ch.service_id is None:
            raise SystemExit("jamnp: set SERVICE_ID (a service seeded empty at genesis)")
        driver = JamnpDriver(ch, sc, env.get("NODE_METRICS_URL", ""), a.timeout)
    cases = [c for c in CASES if not a.only or c[0] in a.only.split(",")]
    keys = state_keys(sc)
    log(f"encrypt-until-batch e2e on {ch.describe()}: {', '.join(c[0] for c in cases)}; "
        f"{'a fresh service per case' if driver.fresh_per_case else 'one service, every case in turn'}")
    results, failed = [], None
    for case in cases:
        try:
            results.append(run_case(ch, sc, keys, driver, case))
        except CaseFailed as e:
            failed = str(e)
            results.append({"case": case[0], "verdict": f"FAILED: {e}"})
            log(f"   FAILED: {e}")
            if not driver.fresh_per_case:
                break                             # the shared service's state is now unknown
    if a.json:
        with open(a.json, "w") as f:
            json.dump({"backend": ch.describe(), "results": results}, f, indent=2)
    print(f"\nencrypt-until-batch e2e — {ch.describe()}")
    for r in results:
        print(f"  {r['case']:<22} {r['verdict']}")
    if failed or len(results) < len(cases):
        print("FAILED")
        return 1
    print("ALL ASSERTIONS PASSED: the honest round settles; tampered, wrong-committee and "
          "injected rounds are rejected with the service state unchanged")
    return 0


if __name__ == "__main__":
    sys.exit(main())
