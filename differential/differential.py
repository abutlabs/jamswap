#!/usr/bin/env python3
"""Cross-client differential — one jamswap service blob, two independent JAM clients.

Runs the IDENTICAL trustless scenario (owner-signed register → list → deposit →
in-refine-verified signed order → FORGED order) on lasair and on PolkaJam, then asserts
the resulting on-chain SERVICE STATE is byte-identical. Any divergence is a conformance
bug in one client (judged against the Graypaper GP 0.7.2 — never "whoever differs from pj").

This is the QUIC-era rewrite of the original driver.py (whose lasair lane used the retired
HTTP operator RPC). The lanes are now STANDALONE so each runs in its own environment and
emits its state as JSON; a third `compare` step diffs the two:

    # lasair lane — inside the lasair6 network, service seeded at genesis (SERVICE_ID)
    BUILDER_URL=http://builder:19980 READER_URL=http://reader:19990 SERVICE_ID=100 \
        python3 differential.py lasair > lasair.json

    # pj lane — inside the pj image, against a fresh polkajam-testnet
    python3 differential.py pj > pj.json

    # verdict
    python3 differential.py compare lasair.json pj.json

Clean-room: PolkaJam is a black box driven only by its public CLI (`jamt`). No internals.
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request

from nacl.signing import SigningKey

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
    client.item(bytes([6]) + struct.pack("<III", MARKET, BASE, QUOTE))         # LIST
    client.item(bytes([1]) + struct.pack("<II", 1, QUOTE) + p64(1000 * S))     # DEPOSIT
    out["handle"] = client.poll(b"h" + pk).hex()
    out["balance"] = client.poll(b"b" + p32(QUOTE) + p32(1)).hex()
    print(f"[{client.name}] registered + funded", file=sys.stderr)
    client.item(smatch([signed_order(sk, 1, 10, 0, 80, 5, 1)]))                # SIGNED order
    book = client.poll(b"book" + p32(MARKET))
    out["book"] = book.hex()
    print(f"[{client.name}] signed order rested", file=sys.stderr)
    client.item(smatch([signed_order(mal, 1, 11, 0, 80, 5, 99)], book))        # FORGED order
    time.sleep(client.settle_secs * 3)      # a rejection writes no new state — fixed wait, then re-read
    out["book_after_forgery"] = client.storage(b"book" + p32(MARKET)).hex()
    return out


# ---- lasair lane: QUIC via the CE-133 builder + CE-129 reader bridges --------
class Lasair:
    name = "lasair"
    settle_secs = 8

    def __init__(self):
        self.builder = os.environ["BUILDER_URL"].rstrip("/")
        self.reader = os.environ["READER_URL"].rstrip("/")
        self.sid = int(os.environ.get("SERVICE_ID", "100"))

    def _post(self, url, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"content-type": "application/json"}, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=60).read())

    def deploy(self):
        # QUIC has no runtime deploy — the service is seeded at genesis; sid is fixed.
        pass

    def item(self, payload):
        r = self._post(self.builder + "/submit", {"service_id": self.sid, "payload_hex": payload.hex()})
        if r.get("accepted") is False:
            raise RuntimeError("builder refused work-item (CE-133 queues full)")

    def storage(self, key):
        req = urllib.request.Request(f"{self.reader}/read?service={self.sid}&key={key.hex()}")
        r = json.loads(urllib.request.urlopen(req, timeout=60).read())
        return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""

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

    def _jamt(self, *args, check=True, timeout=120):
        return subprocess.run([self.jamt, *args], capture_output=True, text=True,
                              timeout=timeout, check=check)

    def deploy(self):
        out = self._jamt("create-service", self.jam, "1000000000", check=False, timeout=300)
        for tok in (out.stdout + out.stderr).split():
            if tok.isalnum() and len(tok) == 8 and tok.startswith("0000"):
                self.sid = str(int(tok, 16))
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
        r = self._jamt("inspect", "storage", "--raw", self.sid, "0x" + key.hex(), check=False, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return bytes.fromhex(out[2:]) if out.startswith("0x") else b""

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
    for check in ("handle", "balance", "book", "book_after_forgery"):
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
