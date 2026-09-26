#!/usr/bin/env python3
"""Cross-client differential — one jamswap service blob, two independent JAM clients.

Runs the IDENTICAL trustless scenario (owner-signed register → list → deposit →
in-refine-verified signed order → FORGED order) on lasair and on PolkaJam, then asserts
the resulting on-chain SERVICE STATE is byte-identical. Any divergence is a conformance
bug in one client (judged against the Graypaper GP 0.8.0 — never "whoever differs from pj").

The lanes are STANDALONE so each runs in its own environment and emits its state as
JSON; a third `compare` step diffs the two. Both lanes read service state through the
DEX's chain adapter (offchain/chain.py):

    # lasair lane — inside the lasair6 network, service seeded at genesis (SERVICE_ID);
    # the adapter's jamnp backend (CE-133 builder + CE-129 reader bridges)
    BUILDER_URL=http://builder:19980 READER_URL=http://reader:19990 SERVICE_ID=100 \
        python3 differential.py lasair > lasair.json

    # pj lane — inside the pj image, against a fresh polkajam-testnet: deploy + items via
    # the public `jamt` CLI (runtime deploy and spec-valid submission are jamswap #13/#11),
    # reads via the adapter's jip2 backend (JIP-2 serviceValue at CHAIN_RPC)
    python3 differential.py pj > pj.json

    # verdict
    python3 differential.py compare lasair.json pj.json

Clean-room: PolkaJam is a black box driven only by its public CLI (`jamt`) and its public
JIP-2 RPC. No internals.
"""
import json
import os
import struct
import subprocess
import sys
import time

from nacl.signing import SigningKey

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "..", "offchain")]   # chain.py: beside us in the image
import chain  # noqa: E402

S = 10_000
MARKET, BASE, QUOTE = 1, 1, 0


def canon(action, *parts):
    return b"jamswap:v1:" + action + b"".join(parts)


def p32(x):
    return struct.pack("<I", x)


def p64(x):
    return struct.pack("<Q", x)


# ---- the identical scenario (client-agnostic) -------------------------------
def signed_order(sk, acct, oid, side, price, qty, seq):
    ob = struct.pack("<IIBII", acct, oid, side, price * S, qty * S)
    msg = canon(b"order", p32(acct), p32(MARKET), bytes([side]), p32(qty * S),
                b"\0", b"\0", p32(price * S), p64(seq))
    return ob + b"\0" + struct.pack("<IQ", price * S, seq) + bytes(sk.verify_key) + sk.sign(msg).signature


def smatch(signed_orders, book=b""):
    sec = struct.pack("<H", len(signed_orders)) + b"".join(signed_orders) \
        + struct.pack("<H", 0) + book
    return bytes([12]) + struct.pack("<III", MARKET, BASE, QUOTE) + sec


def run_scenario(client):
    """Drive the identical work-item sequence on `client`; return the state dict it
    settled. `client` implements deploy()/item(payload)/storage(key)/poll(key)."""
    sk = SigningKey(b"differential-trader-fixed-key-01")
    mal = SigningKey(b"mallory-not-the-real-trader-key!")
    pk = bytes(sk.verify_key)
    out = {}
    client.deploy()
    print(f"[{client.name}] service id {client.sid}", file=sys.stderr)
    client.item(bytes([7]) + pk + sk.sign(canon(b"register", pk)).signature)   # REGISTER
    # the chain assigns the account handle: 1 on a fresh chain, but a genesis that seeds
    # dev accounts (lasair6 seeds 1-6) assigns the next free one — use what it assigned
    handle = client.poll(b"h" + pk)
    out["handle"] = handle.hex()
    acct = struct.unpack("<I", handle[:4])[0] if len(handle) >= 4 else 1
    client.item(bytes([6]) + struct.pack("<III", MARKET, BASE, QUOTE))         # LIST
    client.item(bytes([1]) + struct.pack("<II", acct, QUOTE) + p64(1000 * S))  # DEPOSIT
    out["balance"] = client.poll(b"b" + p32(QUOTE) + p32(acct)).hex()
    print(f"[{client.name}] registered as account {acct} + funded", file=sys.stderr)
    client.item(smatch([signed_order(sk, acct, 10, 0, 80, 5, 1)]))             # SIGNED order
    book = client.poll(b"book" + p32(MARKET))
    out["book"] = book.hex()
    print(f"[{client.name}] signed order " + ("rested" if book else "NOT on the book"), file=sys.stderr)
    client.item(smatch([signed_order(mal, acct, 11, 0, 80, 5, 99)], book))     # FORGED order
    time.sleep(client.settle_secs * 3)      # a rejection writes no new state — fixed wait, then re-read
    out["book_after_forgery"] = client.storage(b"book" + p32(MARKET)).hex()
    return out


# ---- lasair lane: the chain adapter (jamnp: CE-133 builder + CE-129 reader) ---
class Lasair:
    name = "lasair"
    settle_secs = 8

    def __init__(self):
        self.chain = chain.from_env()          # CHAIN_BACKEND (default jamnp) + SERVICE_ID
        if self.chain.service_id is None:
            self.chain.service_id = 100        # lasair6's genesis-seeded id
        self.sid = self.chain.service_id

    def deploy(self):
        # no runtime deploy yet (jamswap #13) — the service is seeded at genesis; sid is fixed.
        pass

    def item(self, payload):
        self.chain.submit(payload)             # raises chain.ChainBusy if every guarantor refused

    def storage(self, key):
        return self.chain.read(key)

    def poll(self, key, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.storage(key)
            if v:
                return v
            time.sleep(6)
        return b""


# ---- polkajam lane: black-box via the jamt CLI ------------------------------
class Polkajam:
    name = "polkajam"
    settle_secs = 30

    def __init__(self):
        self.jamt = os.environ.get("JAMT", "/usr/local/bin/jamt")
        self.jam = os.environ.get("JAM", "/work/jamswap-service.jam")
        # reads go through the chain adapter's JIP-2 backend; the id is set by deploy()
        self.chain = chain.Jip2Chain(url=os.environ.get("CHAIN_RPC") or "ws://localhost:19800")

    def _jamt(self, *args, check=True, timeout=120):
        return subprocess.run([self.jamt, *args], capture_output=True, text=True,
                              timeout=timeout, check=check)

    def deploy(self):
        # --raw: jamt prints only the new service id (8 hex digits) on stdout; since
        # jamt 0.1.29 the id is not printed at all without it
        out = self._jamt("create-service", "--raw", self.jam, "1000000000", check=False, timeout=300)
        for tok in out.stdout.split():
            if len(tok) == 8 and all(c in "0123456789abcdefABCDEF" for c in tok):
                self.sid = str(int(tok, 16))
                self.chain.service_id = int(tok, 16)
                time.sleep(25)     # let the create anchor before items reference it
                return
        raise RuntimeError(f"create-service failed: {out.stdout} {out.stderr}")

    def item(self, payload):
        self._jamt("item", "-G", "100000000", "-g", "9000000", self.sid, "0x" + payload.hex())
        # pj drops work-items submitted back-to-back (they race for the same core/anchor),
        # so let each one anchor + start accumulating before the next. Without this the
        # first item (register) is silently lost while later ones land — a HARNESS bug that
        # masquerades as a conformance divergence. Confirmed live: a spaced register settles
        # in ~12s; three unspaced items drop the first.
        time.sleep(10)

    def storage(self, key):
        return self.chain.read(key)            # JIP-2 serviceValue at the best block

    def poll(self, key, timeout=180):
        deadline = time.time() + timeout
        while time.time() < deadline:
            v = self.storage(key)
            if v:
                return v
            time.sleep(6)
        return b""


def compare(a_path, b_path):
    a, b = json.load(open(a_path)), json.load(open(b_path))
    na, nb = a.get("_client", "A"), b.get("_client", "B")
    print(f"\n{'check':<20} {na:<44} {nb:<44} verdict")
    ok = True
    # the handle is chain-assigned (genesis-seeded accounts shift it), so book entries are
    # compared with their account field replaced by the lane's own handle marker
    def norm(book_hex, handle_hex):
        recs = [book_hex[i:i + 34] for i in range(0, len(book_hex), 34)]   # 17-byte orders
        return "".join(("<own>" + r[8:]) if r[:8] == handle_hex else r for r in recs)
    for side in (a, b):
        for k in ("book", "book_after_forgery"):
            side[k] = norm(side.get(k, ""), side.get("handle", "")[:8])
    print(f"handles (chain-assigned, informational): {na} {a.get('handle')}  {nb} {b.get('handle')}")
    for check in ("balance", "book", "book_after_forgery"):
        va, vb = a.get(check, ""), b.get(check, "")
        same = va == vb and va != ""
        ok &= same
        print(f"{check:<20} {(va or '(empty)'):<44} {(vb or '(empty)'):<44} "
              f"{'MATCH ✓' if same else 'DIVERGED ✗'}")
    forged_rejected = a.get("book") == a.get("book_after_forgery") and b.get("book") == b.get("book_after_forgery")
    print(f"\nforged order rejected on both: {'✓' if forged_rejected else '✗'}")
    print("DIFFERENTIAL: " + ("ALL CLIENTS AGREE — byte-identical service state" if ok and forged_rejected
                              else "DIVERGENCE FOUND — a conformance bug in one client"))
    return 0 if ok and forged_rejected else 1


def main():
    if len(sys.argv) >= 2 and sys.argv[1] == "compare":
        sys.exit(compare(sys.argv[2], sys.argv[3]))
    lane = sys.argv[1] if len(sys.argv) >= 2 else "lasair"
    client = {"lasair": Lasair, "pj": Polkajam, "polkajam": Polkajam}[lane]()
    state = run_scenario(client)
    state["_client"] = client.name
    print(json.dumps(state, indent=2))


if __name__ == "__main__":
    main()
