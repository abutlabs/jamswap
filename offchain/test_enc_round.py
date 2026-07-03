#!/usr/bin/env python3
"""E2E: encrypt-until-batch (option 2) on a fresh lasair-node.

Proves the sealed-encrypted round settles honestly and that every builder attack is
rejected — each adversarial round runs against a FULL committed set so it triggers only
its own defense:
  1. honest committee decryption            -> settles (buyer receives base, encset consumed)
  2. tampered Chaum-Pedersen proof          -> refine rejects (empty output, no settlement)
  3. wrong committee keys                   -> accumulate rejects (committee_hash mismatch)
  4. injected uncommitted ciphertext        -> accumulate consume-or-reject
"""
import json, os, struct, subprocess, urllib.request

RPC = os.environ.get("LASAIR_RPC", "http://127.0.0.1:19903")
COMMITTEE = os.environ["COMMITTEE"]  # path to committee binary
JAM = os.environ["JAM"]
MARKET, BASE, QUOTE, SCALE = 1, 10, 20, 10_000

def rpc(method, path, body=None):
    req = urllib.request.Request(RPC + path, method=method,
        data=json.dumps(body).encode() if body else None,
        headers={"content-type": "application/json"})
    return json.loads(urllib.request.urlopen(req, timeout=60).read() or "null")

def u32(x): return struct.pack("<I", x)
def u64(x): return struct.pack("<Q", x)

def submit(sid, payload_hex):
    return rpc("POST", f"/v1/service/{sid}/item", {"payload_hex": payload_hex})

def storage(sid, key):
    v = rpc("GET", f"/v1/service/{sid}/storage/{key.hex()}")
    if isinstance(v, dict): v = v.get("value_hex")
    return bytes.fromhex(v) if v else b""

def bal(sid, asset, account):
    v = storage(sid, b"b" + u32(asset) + u32(account))
    return struct.unpack("<Q", v.ljust(8, b"\0"))[0] if v else 0

def encset(sid):
    return storage(sid, b"encset" + u32(MARKET))

def scenario():
    out = subprocess.run([COMMITTEE, "scenario", "0"], capture_output=True, text=True, check=True).stdout
    return {line.split()[0]: line.split()[1] for line in out.strip().splitlines()}

def setup_chain(sid, s):
    # register the trader keys FIRST (registration order fixes handles: buyer → 1, seller → 2);
    # sealed commits are owner-signed, so unregistered accounts can't commit at all.
    submit(sid, s["register_buy"])
    submit(sid, s["register_sell"])
    submit(sid, (bytes([6]) + u32(MARKET) + u32(BASE) + u32(QUOTE)).hex())        # LIST
    submit(sid, (bytes([1]) + u32(1) + u32(QUOTE) + u64(10_000 * SCALE)).hex())   # fund buyer quote
    submit(sid, (bytes([1]) + u32(2) + u32(BASE) + u64(10_000 * SCALE)).hex())    # fund seller base

def commit_pair(sid, s):
    submit(sid, s["commit_buy"])
    submit(sid, s["commit_sell"])

def deploy_fresh(s):
    sid = rpc("POST", "/v1/service", {"jam_hex": open(JAM, "rb").read().hex()})["service_id"]
    setup_chain(sid, s)
    submit(sid, s["setup"])
    assert len(storage(sid, b"committee")) == 1 + 2 * 32, "committee must be committed on-chain"
    return sid

def main():
    s = scenario()

    # --- attacks (each on a fresh chain with a full committed set) ---
    for name, why in [("round_tampered", "refine rejects a bad proof"),
                      ("round_wrongcommittee", "accumulate rejects a swapped committee"),
                      ("round_injected", "accumulate consume-or-reject on an uncommitted ciphertext")]:
        sid = deploy_fresh(s)
        commit_pair(sid, s)
        before = (bal(sid, BASE, 1), bal(sid, QUOTE, 2))
        n_before = len(encset(sid))
        submit(sid, s[name])
        after = (bal(sid, BASE, 1), bal(sid, QUOTE, 2))
        assert after == (0, 0) == before, f"{name}: NO settlement expected, got {after}"
        assert len(encset(sid)) == n_before, f"{name}: encset must be untouched"
        print(f"{name:22s} REJECTED, no settlement, encset intact  ({why})")

    # --- honest round settles ---
    sid = deploy_fresh(s)
    commit_pair(sid, s)
    assert len(encset(sid)) == 2 * 36, "two ciphertexts committed"
    r = submit(sid, s["round"])
    buyer_base = bal(sid, BASE, 1)
    seller_quote = bal(sid, QUOTE, 2)
    FEE = 300  # flat per-filled-order fee in the base asset (FEE_FLAT in the service)
    assert buyer_base == 5 * SCALE - FEE, f"buyer must receive 5 base − fee, got {buyer_base}"
    assert seller_quote > 0, f"seller must receive quote proceeds, got {seller_quote}"
    assert encset(sid) == b"", "both ciphertexts consumed by the honest round"
    print(f"round (honest)         SETTLED — buyer +{buyer_base} base, seller +{seller_quote} quote, "
          f"encset consumed  (verdict={r.get('verdict')})")
    print("\nALL ASSERTIONS PASSED — encrypt-until-batch verified e2e on lasair "
          "(honest decrypts; tampered / wrong-committee / injected all rejected)")

if __name__ == "__main__":
    main()
