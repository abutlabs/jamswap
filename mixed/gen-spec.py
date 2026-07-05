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
  - lasair index i:    bandersnatch = lasair Bandersnatch(seed chr(i+5)) (its
                       hardcoded sealing key for index i); peer_id = lasair
                       N(k)="e"++base32_le(ed25519,52) for its QUIC identity seed
                       chr(100+i). Both come from `mixed_keys` (from the lasair
                       image, present in this container).

Env:
  LAYOUT     comma list of clients per index (default polkajam,polkajam,polkajam,
             lasair,lasair,lasair). Length must equal the config size (6 = tiny).
  SHARED     output dir (default /shared)
  POLKAJAM   polkajam binary (default polkajam on PATH)
  MIXED_KEYS lasair mixed_keys binary (default mixed_keys on PATH)
  BASE_PORT  first JAMNP-S UDP port (default 40060); index i -> BASE_PORT+i
  RPC_BASE   first PolkaJam RPC port (default 19890); index i -> RPC_BASE+i
"""
import os, sys, re, json, glob, time, shutil, subprocess

SHARED   = os.environ.get("SHARED", "/shared")
POLKAJAM = os.environ.get("POLKAJAM", "polkajam")
MIXED    = os.environ.get("MIXED_KEYS", "mixed_keys")
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

def mixed_keys(seed_hex):
    out = subprocess.run([MIXED, seed_hex], capture_output=True, text=True).stdout
    g = lambda k: [l.split()[1] for l in out.splitlines() if l.startswith(k)][0]
    return g("bandersnatch:"), g("peer_id:")

# PolkaJam gen-keys writes the seed into its keystore dir with a timestamped name;
# capture the newest .seed after each call.
def pj_keystore_dir():
    # PolkaJam writes gen-keys seeds under its config dir, which varies by OS:
    #   Linux:  ~/.config/polkajam/polkadot/keys
    #   macOS:  ~/Library/Application Support/polkajam/polkadot/keys
    for base in (os.path.expanduser("~/.config/polkajam"),
                 os.path.expanduser("~/.local/share/polkajam"),
                 os.path.expanduser("~/Library/Application Support/polkajam")):
        d = os.path.join(base, "polkadot", "keys")
        if os.path.isdir(d):
            return d
    return None

def gen_pj_key(idx):
    # snapshot keystore, gen a key, diff to find the new seed file
    ks = pj_keystore_dir()
    before = set(glob.glob(os.path.join(ks, "*.seed"))) if ks else set()
    out = subprocess.run([POLKAJAM, "gen-keys"], capture_output=True, text=True).stdout
    pid = re.search(r"Peer ID:\s*(\S+)", out).group(1)
    ban = re.search(r"Bandersnatch key:\s*(\S+)", out).group(1)
    time.sleep(0.08)
    ks = ks or pj_keystore_dir()
    seeds = glob.glob(os.path.join(ks, "*.seed")) if ks else []
    newf = sorted(set(seeds) - before)
    seed = newf[-1] if newf else (max(seeds, key=os.path.getmtime) if seeds else None)
    if not seed:
        sys.exit("gen-keys produced no seed file; keystore=%s" % ks)
    dst = os.path.join(SHARED, "pj_%d.seed" % idx)
    shutil.copy(seed, dst)
    return pid, ban, dst

vals, nodes = [], []
for i, role in enumerate(layout):
    port = BASE + i
    host = ip_for(i)                    # numeric IP (gen-spec + dialing require it)
    net  = "%s:%d" % (host, port)
    if role == "lasair":
        seal  = ("%02x" % (i + 5)) * 32     # lasair index i seals with seed chr(i+5)
        ident = ("%02x" % (100 + i)) * 32   # lasair QUIC identity seed for this node
        ban, _   = mixed_keys(seal)
        _, pid   = mixed_keys(ident)
        vals.append({"peer_id": pid, "bandersnatch": ban, "net_addr": net})
        nodes.append({"index": i, "role": "lasair", "host": host, "port": port,
                      "peer_id": pid, "identity": 100 + i, "own": i})
    elif role == "polkajam":
        pid, ban, seed = gen_pj_key(i)
        vals.append({"peer_id": pid, "bandersnatch": ban, "net_addr": net})
        nodes.append({"index": i, "role": "polkajam", "host": host, "port": port,
                      "rpc": RPCBASE + i, "peer_id": pid,
                      "seed": os.path.basename(seed), "own": i})
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

spec = json.load(open(spec_path))
assert "genesis_header" in spec and "genesis_state" in spec, list(spec.keys())
open(os.path.join(SHARED, "ready"), "w").write("ok")
print("mixed genesis ready: %d validators (%s), %d state entries; bootnode %s"
      % (len(vals), LAYOUT, len(spec["genesis_state"]), topo["bootnode"]))
