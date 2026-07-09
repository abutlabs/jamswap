#!/usr/bin/env python3
"""Generate the SHARED genesis spec for a mixed lasair+PolkaJam JAM chain.

Run once (the `spec-init` service) before the validators start. It decides, per
validator index, which client owns it, derives that validator's genesis entry with
the OWNING client's real keys, and writes:

  /shared/spec.json   the gen-spec genesis (genesis_header + genesis_state) BOTH
                      clients load — identical bytes, identical state root;
  /shared/nodes.json  the topology every entrypoint reads (index -> role, host,
                      port, rpc, peer_id, identity, own);
  /shared/pj_<i>.seed the PolkaJam seed for each PolkaJam-owned index (its node
                      loads it with --key-seed-file);
  /shared/ready       written last, so entrypoints can wait on it.

Key ownership stays split: each validator's genesis entry carries the OWNING
client's public keys, and only that client ever holds the secret.
  - PolkaJam index i:  `gen-keys` -> peer_id + bandersnatch + a 32-byte seed file.
  - lasair index i:    the STANDARD JAM dev account i (docs.jamcha.in/basics/
                       dev-accounts, seed = u32-LE index x8): bandersnatch is its
                       sealing key and peer_id its ed25519 QUIC identity — exactly
                       the keys a lasair >=1.5.1 validator derives for --own i.
                       Both come from `lasair --dev-account` (the lasair client
                       binary, copied into this container from its image).

Env:
  LAYOUT     comma list of clients per index (default polkajam,polkajam,polkajam,
             lasair,lasair,lasair). Length must equal the config size (6 = tiny).
  SHARED     output dir (default /shared)
  POLKAJAM   polkajam binary (default polkajam on PATH)
  LASAIR_BIN lasair client binary (default lasair on PATH)
  BASE_PORT  first JAMNP-S UDP port (default 40060); index i -> BASE_PORT+i
  RPC_BASE   first PolkaJam RPC port (default 19890); index i -> RPC_BASE+i
"""
import os, sys, json, subprocess

SHARED   = os.environ.get("SHARED", "/shared")
POLKAJAM = os.environ.get("POLKAJAM", "polkajam")
LASAIR   = os.environ.get("LASAIR_BIN", "lasair")
LAYOUT   = os.environ.get("LAYOUT", "polkajam,polkajam,polkajam,lasair,lasair,lasair")
BASE     = int(os.environ.get("BASE_PORT", "40060"))
RPCBASE  = int(os.environ.get("RPC_BASE", "19890"))
# PolkaJam gen-spec requires a NUMERIC socket addr, so every node gets a static IP
# on the compose network: index i -> IPBASE.(IPSTART+i). The compose file pins the
# same IPs. lasair dials peers by these IPs too.
IPBASE   = os.environ.get("IP_BASE", "172.28.0")
IPSTART  = int(os.environ.get("IP_START", "10"))
layout   = [x.strip() for x in LAYOUT.split(",") if x.strip()]

def ip_for(i):
    return "%s.%d" % (IPBASE, IPSTART + i)

os.makedirs(SHARED, exist_ok=True)

# IDEMPOTENT: a partial `docker compose up` (e.g. only the lm services recreated
# after an LASAIR_IMAGE change) re-runs this one-shot init. Minting a NEW genesis
# then splits the project in two — the still-running validators keep the old chain
# and every cross-half QUIC dial dies with "peer doesn't support any known
# protocol" (the ALPN embeds the genesis hash). If a genesis already exists in
# the shared volume, REUSE it; `docker compose down -v` wipes it for a fresh net.
if os.path.exists(os.path.join(SHARED, "ready")) and \
   os.path.exists(os.path.join(SHARED, "spec.json")):
    print("spec-init: /shared/spec.json exists — reusing the running genesis "
          "(docker compose down -v for a fresh one)")
    sys.exit(0)

def lasair_dev_account(i):
    # `lasair --dev-account i` prints the OFFICIAL JAM dev account (docs.jamcha.in) —
    # the exact bandersnatch sealing key and ed25519 QUIC identity a lasair >=1.5.1
    # validator derives for --own i. (mixed_keys' raw public_from_seed derivation
    # yields DIFFERENT keys for the same seed hex — do not substitute it here.)
    out = subprocess.run([LASAIR, "--dev-account", str(i)], capture_output=True, text=True).stdout
    g = lambda k: [l.split()[1] for l in out.splitlines() if l.startswith(k)][0]
    return g("bandersnatch:"), g("peer_id:")

def dev_seed_file(idx):
    """Write the STANDARD JAM dev-account seed (u32-LE(idx) repeated 8x, raw 32
    bytes) where the mixed PolkaJam node loads it (--key-seed-file $SHARED/pj_i.seed).

    PolkaJam derives BYTE-FOR-BYTE the same bandersnatch key and peer_id from this
    seed as `lasair --dev-account idx` (verified for every index) — the dev accounts
    are a shared JAM standard both clients implement identically. Keying the PolkaJam
    validators this way (vs random `gen-keys`) is what makes a work-report AVAILABLE
    on a 3:3 mixed chain: availability needs a >2/3 super-majority of assurances (5 of
    6), but a lone lasair guarantor only holds the lasair seeds. With every validator
    keyed to a dev account, the guarantor — which derives all six dev-account secrets —
    forges VALID assurances for the PolkaJam validators too, so the report crosses the
    threshold and accumulates. PolkaJam still runs as the independent CLIENT; only its
    KEY is a well-known dev account (exactly as on the all-lasair devnet)."""
    dst = os.path.join(SHARED, "pj_%d.seed" % idx)
    with open(dst, "wb") as f:
        f.write((idx.to_bytes(4, "little")) * 8)
    return dst

vals, nodes = [], []
for i, role in enumerate(layout):
    port = BASE + i
    host = ip_for(i)                    # numeric IP (gen-spec + dialing require it)
    net  = "%s:%d" % (host, port)
    if role == "lasair":
        # STANDARD JAM dev account i (docs.jamcha.in/basics/dev-accounts): seed is the
        # index as a u32 LE repeated 8x. Since lasair client-v1.5.1 a validator seals
        # with THIS key and presents THIS ed25519 as its QUIC identity — baking anything
        # else makes every lasair seal bad_seal and every lasair cert a stranger.
        ban, pid = lasair_dev_account(i)
        vals.append({"peer_id": pid, "bandersnatch": ban, "net_addr": net})
        nodes.append({"index": i, "role": "lasair", "host": host, "port": port,
                      "peer_id": pid, "identity": 100 + i, "own": i})
    elif role == "polkajam":
        # PolkaJam validator keyed by the SAME standard dev account a lasair validator
        # would use (identical derivation, see dev_seed_file). It still runs the
        # PolkaJam CLIENT — only the key is a well-known dev account, which lets the
        # lasair guarantor forge valid availability assurances for it (>2/3 threshold).
        ban, pid = lasair_dev_account(i)
        dev_seed_file(i)
        vals.append({"peer_id": pid, "bandersnatch": ban, "net_addr": net})
        nodes.append({"index": i, "role": "polkajam", "host": host, "port": port,
                      "rpc": RPCBASE + i, "peer_id": pid,
                      "seed": "pj_%d.seed" % i, "own": i})
    else:
        sys.exit("unknown client in LAYOUT: %r" % role)

cfg = {"id": "jamswap-mixed", "genesis_validators": vals}
cfg_path = os.path.join(SHARED, "cfg.json")
json.dump(cfg, open(cfg_path, "w"))

spec_path = os.path.join(SHARED, "spec.json")
r = subprocess.run([POLKAJAM, "gen-spec", cfg_path, spec_path],
                   capture_output=True, text=True)
if r.returncode != 0 or not os.path.exists(spec_path):
    sys.exit("gen-spec failed: %s\n%s" % (r.stdout, r.stderr))

# bootnode = the first PolkaJam validator (fallback: first node)
boot = next((n for n in nodes if n["role"] == "polkajam"), nodes[0])
topo = {"nodes": nodes, "bootnode": "%s@%s:%d" % (boot["peer_id"], boot["host"], boot["port"]),
        "base_port": BASE, "rpc_base": RPCBASE}
json.dump(topo, open(os.path.join(SHARED, "nodes.json"), "w"), indent=2)

# Per-node peer list WITH each peer's peer_id (ip:port@peer_id). lasair reads its own
# file and applies the JAMNP-S Preferred Initiator: exactly one side of every pair
# dials, so a lasair<->PolkaJam link never churns. PolkaJam peer_ids are random per
# run, hence resolved here rather than hardcoded in the compose file.
for n in nodes:
    peers = ["%s:%d@%s" % (m["host"], m["port"], m["peer_id"])
             for m in nodes if m["index"] != n["index"]]
    open(os.path.join(SHARED, "peers_%d.txt" % n["index"]), "w").write(",".join(peers))

spec = json.load(open(spec_path))
assert "genesis_header" in spec and "genesis_state" in spec, list(spec.keys())

# Seed the jamswap service into the SHARED genesis (the genesis-config "deploy"
# for a mixed chain): every client — lasair AND PolkaJam — then starts with the
# service on-chain. State-only: the genesis_header (and so the genesis hash the
# ALPN embeds) is unchanged. Skipped when no SERVICE is mounted.
service = os.environ.get("SERVICE", "")
if service and os.path.exists(service):
    inject = [LASAIR, "--inject-service-spec", spec_path,
              "--service", service,
              "--service-id", os.environ.get("SERVICE_ID", "100")]
    # GENESIS ACCOUNTS: pre-register the six standard JAM dev accounts (Alice..Fergie
    # — the same well-known ed25519 keys the trading UI holds) and fund each with
    # GENESIS_BALANCE of every asset, directly in the service's genesis storage. A
    # fresh devnet is then tradable at slot 1 — no minutes of on-chain register/
    # deposit settlement before load tests or UI sessions can start. Storage layout
    # matches service/src/lib.rs exactly: b"h"+pub->handle, b"pk"+handle->pub,
    # b"nexthandle"->next u32, b"b"+asset+handle->u64 atomic, b"cust"+asset->u64.
    # GENESIS_BALANCE=0 disables. Handles 1..6 in account order below.
    gbal = int(os.environ.get("GENESIS_BALANCE", "1000000"))     # display units/asset
    if gbal > 0:
        SCALE, ASSETS = 10_000, (0, 1, 2)                        # USDC, DOT, JAMKB
        DEV_PUBS = [   # docs.jamcha.in dev accounts, as held by offchain/web/index.html
            "4418fb8c85bb3985394a8c2756d3643457ce614546202a2f50b093d762499ace",  # Alice
            "ad93247bd01307550ec7acd757ce6fb805fcf73db364063265b30a949e90d933",  # Bob
            "cab2b9ff25c2410fbe9b8a717abb298c716a03983c98ceb4def2087500b8e341",  # Carol
            "f30aa5444688b3cab47697b37d5cac5707bb3289e986b19b17db437206931a8d",  # David
            "8b8c5d436f92ecf605421e873a99ec528761eb52a88a2f9a057b3b3003e6f32a",  # Eve
            "ab0084d01534b31c1dd87c81645fd762482a90027754041ca1b56133d0466c06",  # Fergie
        ]
        atomic = gbal * SCALE
        kv = {}
        for h, pub in enumerate(DEV_PUBS, start=1):
            p = bytes.fromhex(pub)
            kv[(b"h" + p).hex()] = h.to_bytes(4, "little").hex()
            kv[(b"pk" + h.to_bytes(4, "little")).hex()] = pub
            for a in ASSETS:
                kv[(b"b" + a.to_bytes(4, "little") + h.to_bytes(4, "little")).hex()] = \
                    atomic.to_bytes(8, "little").hex()
        kv[b"nexthandle".hex()] = (len(DEV_PUBS) + 1).to_bytes(4, "little").hex()
        for a in ASSETS:                     # custody invariant: cust = sum of balances
            kv[(b"cust" + a.to_bytes(4, "little")).hex()] = \
                (atomic * len(DEV_PUBS)).to_bytes(8, "little").hex()
        seed_path = os.path.join(SHARED, "genesis_accounts.json")
        json.dump(kv, open(seed_path, "w"))
        inject += ["--seed-service-storage", seed_path]
        print("genesis accounts: %d dev accounts pre-registered, %s of each asset"
              % (len(DEV_PUBS), gbal))
    r = subprocess.run(inject, capture_output=True, text=True)
    if r.returncode != 0:
        sys.exit("service injection failed: %s\n%s" % (r.stdout, r.stderr))
    print(r.stdout.strip())
    spec = json.load(open(spec_path))

# The ALPN embeds the genesis hash = blake2b256(genesis_header bytes); write its
# 4-byte prefix so off-chain bridges (jamnp-builder / lasair-reader) can match
# the chain without a per-genesis hardcoded env.
import hashlib
gh = bytes.fromhex(spec["genesis_header"])
open(os.path.join(SHARED, "genesis_hex"), "w").write(
    hashlib.blake2b(gh, digest_size=32).hexdigest()[:8])

open(os.path.join(SHARED, "ready"), "w").write("ok")
print("mixed genesis ready: %d validators (%s), %d state entries; bootnode %s"
      % (len(vals), LAYOUT, len(spec["genesis_state"]), topo["bootnode"]))
