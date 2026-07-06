#!/usr/bin/env python3
"""Cross-client differential driver: one jamswap blob, two independent JAM clients.

Deploys the SAME service/jamswap-service.jam to a lasair node (HTTP operator RPC)
and a PolkaJam node (jamt CLI), drives the IDENTICAL trustless scenario on both —
owner-signed registration, market listing, deposit, an in-refine-verified signed
order, then a FORGED order — and asserts the resulting on-chain state is
byte-identical:

  register  -> handle bytes equal
  deposit   -> balance bytes equal
  signed    -> resting-book bytes equal (order rests on both)
  forged    -> book unchanged on both (rejected by both PVMs)

Any divergence is a conformance bug in one of the clients (or in the service's
assumptions) — exactly what this rig exists to find. First green run: 2026-07-04,
lasair-node vs polkajam 0.1.28, both GP 0.7.2.

Env: LASAIR_RPC (default http://lasair-node:19900), JAMT (default /usr/local/bin/jamt),
JAM (default /work/jamswap-service.jam).
"""
import json
import os
import struct
import subprocess
import sys
import time
import urllib.request

from nacl.signing import SigningKey

LASAIR_RPC = os.environ.get("LASAIR_RPC", "http://lasair-node:19900").rstrip("/")
JAMT = os.environ.get("JAMT", "/usr/local/bin/jamt")
JAM = os.environ.get("JAM", "/work/jamswap-service.jam")
S = 10_000
MARKET, BASE, QUOTE = 1, 1, 0


def canon(action, *parts):
    return b"jamswap:v1:" + action + b"".join(parts)


def p32(x):
    return struct.pack("<I", x)


def p64(x):
    return struct.pack("<Q", x)


# ---- lasair lane (HTTP operator RPC) ---------------------------------------
def las_rpc(path, body=None):
    req = urllib.request.Request(LASAIR_RPC + path,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"content-type": "application/json"},
                                 method="POST" if body is not None else "GET")
    return json.loads(urllib.request.urlopen(req, timeout=60).read())


class Lasair:
    name = "lasair"

    def deploy(self):
        self.sid = int(las_rpc("/v1/service", {"jam_hex": open(JAM, "rb").read().hex()})["service_id"])

    def item(self, payload):
        las_rpc(f"/v1/service/{self.sid}/item", {"payload_hex": payload.hex()})

    def storage(self, key):
        r = las_rpc(f"/v1/service/{self.sid}/storage/{key.hex()}")
        return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""


# ---- polkajam lane (jamt CLI; payloads/keys MUST be 0x-prefixed) ------------
class Polkajam:
    name = "polkajam"

    def deploy(self):
        out = subprocess.run([JAMT, "create-service", JAM, "1000000000"],
                             capture_output=True, text=True, timeout=300)
        for tok in (out.stdout + out.stderr).split():
            if tok.isalnum() and len(tok) == 8 and tok.startswith("0000"):
                self.sid = str(int(tok, 16))
                # a work package anchored on a block where the service doesn't exist yet
                # is silently dropped — wait a few slots so items anchor PAST creation
                # (found the hard way: the anchor lagged the creation slot by one).
                time.sleep(25)
                return
        raise RuntimeError(f"create-service failed: {out.stdout} {out.stderr}")

    def item(self, payload):
        subprocess.run([JAMT, "item", "-G", "100000000", "-g", "9000000",
                        self.sid, "0x" + payload.hex()],
                       capture_output=True, text=True, timeout=120, check=True)

    def storage(self, key):
        r = subprocess.run([JAMT, "inspect", "storage", "--raw", self.sid, "0x" + key.hex()],
                           capture_output=True, text=True, timeout=60)
        out = (r.stdout + r.stderr).strip()
        return bytes.fromhex(out[2:]) if out.startswith("0x") else b""


# ---- the identical scenario --------------------------------------------------
def signed_order(sk, acct, oid, side, price, qty, seq):
    ob = struct.pack("<IIBII", acct, oid, side, price * S, qty * S)
    msg = canon(b"order", p32(acct), p32(MARKET), bytes([side]), p32(qty * S),
                b"\0", b"\0", p32(price * S), p64(seq))
    return ob + b"\0" + struct.pack("<IQ", price * S, seq) + bytes(sk.verify_key) + sk.sign(msg).signature


def smatch(signed_orders, book=b""):
    sec = struct.pack("<H", len(signed_orders)) + b"".join(signed_orders) \
        + struct.pack("<H", 0) + book
    return bytes([12]) + struct.pack("<III", MARKET, BASE, QUOTE) + sec


def poll(client, key, timeout=120):
    """Read storage until a value appears (settlement lands slots after submission)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        v = client.storage(key)
        if v:
            return v
        time.sleep(6)
    return b""


def run_scenario(client, settle_secs):
    sk = SigningKey(b"differential-trader-fixed-key-01")
    mal = SigningKey(b"mallory-not-the-real-trader-key!")
    pk = bytes(sk.verify_key)
    out = {}
    client.deploy()
    print(f"[{client.name}] service id {client.sid}")
    client.item(bytes([7]) + pk + sk.sign(canon(b"register", pk)).signature)   # REGISTER
    client.item(bytes([6]) + struct.pack("<III", MARKET, BASE, QUOTE))         # LIST
    client.item(bytes([1]) + struct.pack("<II", 1, QUOTE) + p64(1000 * S))     # DEPOSIT
    out["handle"] = poll(client, b"h" + pk)
    out["balance"] = poll(client, b"b" + p32(QUOTE) + p32(1))
    print(f"[{client.name}] registered + funded")
    client.item(smatch([signed_order(sk, 1, 10, 0, 80, 5, 1)]))                # SIGNED order
    out["book"] = poll(client, b"book" + p32(MARKET))
    print(f"[{client.name}] signed order rested")
    client.item(smatch([signed_order(mal, 1, 11, 0, 80, 5, 99)], out["book"]))  # FORGED order
    time.sleep(settle_secs * 3)  # rejection leaves no new state — fixed wait, then re-read
    out["book_after_forgery"] = client.storage(b"book" + p32(MARKET))
    return out


def main():
    results = {}
    for client, settle in ((Lasair(), 8), (Polkajam(), 30)):
        results[client.name] = run_scenario(client, settle)
    print(f"\n{'check':<20} {'lasair':<40} {'polkajam':<40} verdict")
    ok = True
    for check in ("handle", "balance", "book", "book_after_forgery"):
        a, b = results["lasair"][check], results["polkajam"][check]
        same = a == b and a != b""
        ok &= same
        print(f"{check:<20} {a.hex() or '(empty)':<40} {b.hex() or '(empty)':<40} "
              f"{'MATCH ✓' if same else 'DIVERGED ✗'}")
    forged_rejected = (results["lasair"]["book"] == results["lasair"]["book_after_forgery"])
    print(f"\nforged order rejected on both: {'✓' if forged_rejected and ok else '✗'}")
    print("DIFFERENTIAL: " + ("ALL CLIENTS AGREE — byte-identical service state" if ok
                              else "DIVERGENCE FOUND — a conformance bug in one client"))
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
