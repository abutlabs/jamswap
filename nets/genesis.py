#!/usr/bin/env python3
"""Mint the SHARED genesis of a jamswap test net (the `spec-init` service).

Runs once before the validators start. For a per-index client layout it writes, to
the shared volume:

  spec.json     the JIP-4 chain spec (genesis_header + genesis_state) every client
                loads: identical bytes, so one genesis hash and one state root;
  cfg.json      the gen-spec input it was made from;
  nodes.json    the node table every entrypoint reads (index -> client, host, port,
                rpc, peer_id, ...);
  peers_<i>.txt node i's peers as host:port@peer_id (lasair dials from it);
  pj_<i>.seed   the dev seed a PolkaJam validator loads with --key-seed-file;
  genesis_hex   the first 4 bytes of the genesis header hash (the JAMNP-S ALPN);
  ready         written last, so entrypoints can wait on it.

Keys: validator i is the STANDARD JAM dev account i (JIP-5, nets/devkeys.py), public
keys from the published table: no client binary is needed to pick them. Each client
starts as `--dev-validator i` (or lasair's `--own i`) and so holds that key. The spec
itself comes from `polkajam gen-spec` (black box: config in, spec out).

lasair is involved only when the layout contains it: its binary then cross-checks the
key table (`lasair --dev-account i`), and it is the tool that writes the jamswap
service into genesis (`--inject-service-spec`, when SERVICE is set).

Env:
  CLIENTS    comma list, one client per validator index (lasair, pj|polkajam,
             pbnjam, javajam|jj). LAYOUT is the older name; CLIENTS wins.
             Default polkajam,polkajam,polkajam,lasair,lasair,lasair.
  SHARED     output dir (default /shared)
  POLKAJAM   polkajam binary (default polkajam on PATH)
  LASAIR_BIN lasair binary (default lasair on PATH; only used if lasair is in the layout
             or SERVICE is set)
  BASE_PORT  JAMNP-S UDP port of validator 0 (default 40060); validator i: BASE_PORT+i
  RPC_BASE   RPC port of validator 0 (default 19890); validator i: RPC_BASE+i
  IP_BASE, IP_START  validator i's address is IP_BASE.(IP_START+i) (default 172.28.0, 10):
             a static IP on the compose network
  HOST_IP    if set, EVERY validator's address is HOST_IP:BASE_PORT+i instead (a net with
             a node running natively on the host, all ports published there)
  SERVICE, SERVICE_ID, GENESIS_BALANCE   seed the jamswap service (see below)
"""
import hashlib
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import devkeys  # noqa: E402

CLIENT_ALIASES = {"lasair": "lasair", "pj": "polkajam", "polkajam": "polkajam",
                  "pbnjam": "pbnjam", "javajam": "javajam", "jj": "javajam"}


def parse_clients(spec):
    """'lasair,pj,jj' -> ['lasair', 'polkajam', 'javajam'] (canonical client names)."""
    out = []
    for raw in spec.split(","):
        name = raw.strip().lower()
        if not name:
            continue
        if name not in CLIENT_ALIASES:
            raise ValueError("unknown client %r (known: %s)"
                             % (raw.strip(), ", ".join(sorted(CLIENT_ALIASES))))
        out.append(CLIENT_ALIASES[name])
    if not out:
        raise ValueError("empty client layout")
    return out


def topology(clients, base_port, rpc_base, host_of):
    """The gen-spec validators and the node table for a layout.

    host_of(i) is validator i's numeric address (gen-spec needs an IP, not a name).
    Node-table fields per client are what that client's entrypoint reads; the lasair
    and polkajam rows keep their original shape (docker-compose.mixed.yml, lasair6)."""
    vals, nodes = [], []
    for i, client in enumerate(clients):
        acct = devkeys.dev_account(i)
        host, port = host_of(i), base_port + i
        vals.append({"peer_id": acct["peer_id"], "bandersnatch": acct["bandersnatch"],
                     "net_addr": "%s:%d" % (host, port)})
        if client == "lasair":
            nodes.append({"index": i, "role": "lasair", "host": host, "port": port,
                          "peer_id": acct["peer_id"], "identity": 100 + i, "own": i})
        elif client == "polkajam":
            nodes.append({"index": i, "role": "polkajam", "host": host, "port": port,
                          "rpc": rpc_base + i, "peer_id": acct["peer_id"],
                          "seed": "pj_%d.seed" % i, "own": i})
        else:
            nodes.append({"index": i, "role": client, "host": host, "port": port,
                          "rpc": rpc_base + i, "peer_id": acct["peer_id"], "own": i})
    return vals, nodes


def bootnode(nodes):
    """The first PolkaJam validator (PolkaJam nodes take a --bootnode), else node 0."""
    boot = next((n for n in nodes if n["role"] == "polkajam"), nodes[0])
    return "%s@%s:%d" % (boot["peer_id"], boot["host"], boot["port"])


def peer_lists(nodes):
    """index -> 'host:port@peer_id,...' of every other node."""
    return {n["index"]: ",".join("%s:%d@%s" % (m["host"], m["port"], m["peer_id"])
                                 for m in nodes if m["index"] != n["index"])
            for n in nodes}


def genesis_accounts(balance, scale=10_000, assets=(0, 1, 2)):
    """Service storage that pre-registers the six dev accounts (Alice..Fergie, handles
    1..6) and funds each with `balance` display units of every asset, so a fresh net is
    tradable at slot 1. Layout matches service/src/lib.rs: b"h"+pub -> handle,
    b"pk"+handle -> pub, b"nexthandle" -> next u32, b"b"+asset+handle -> u64 atomic,
    b"cust"+asset -> u64 (custody = the sum of balances)."""
    atomic, kv = balance * scale, {}
    pubs = [row[1] for row in devkeys.PUBLISHED]
    for h, pub in enumerate(pubs, start=1):
        p = bytes.fromhex(pub)
        kv[(b"h" + p).hex()] = h.to_bytes(4, "little").hex()
        kv[(b"pk" + h.to_bytes(4, "little")).hex()] = pub
        for a in assets:
            kv[(b"b" + a.to_bytes(4, "little") + h.to_bytes(4, "little")).hex()] = \
                atomic.to_bytes(8, "little").hex()
    kv[b"nexthandle".hex()] = (len(pubs) + 1).to_bytes(4, "little").hex()
    for a in assets:
        kv[(b"cust" + a.to_bytes(4, "little")).hex()] = \
            (atomic * len(pubs)).to_bytes(8, "little").hex()
    return kv


def check_against_lasair(lasair, n):
    """Fail if lasair derives other keys than the table (it signs with its own)."""
    for i in range(n):
        out = subprocess.run([lasair, "--dev-account", str(i)],
                             capture_output=True, text=True).stdout
        got = {l.split()[0].rstrip(":"): l.split()[1] for l in out.splitlines() if l.strip()}
        want = devkeys.dev_account(i)
        for k in ("bandersnatch", "ed25519", "peer_id"):
            if got.get(k) != want[k]:
                sys.exit("dev account %d: lasair --dev-account %s = %s, table = %s"
                         % (i, k, got.get(k), want[k]))


def main():
    shared = os.environ.get("SHARED", "/shared")
    polkajam = os.environ.get("POLKAJAM", "polkajam")
    lasair = os.environ.get("LASAIR_BIN", "lasair")
    layout = os.environ.get("CLIENTS") or os.environ.get(
        "LAYOUT", "polkajam,polkajam,polkajam,lasair,lasair,lasair")
    base = int(os.environ.get("BASE_PORT", "40060"))
    rpc_base = int(os.environ.get("RPC_BASE", "19890"))
    ip_base = os.environ.get("IP_BASE", "172.28.0")
    ip_start = int(os.environ.get("IP_START", "10"))
    host_ip = os.environ.get("HOST_IP", "")
    try:
        clients = parse_clients(layout)
    except ValueError as e:
        sys.exit("spec-init: %s" % e)

    os.makedirs(shared, exist_ok=True)
    # IDEMPOTENT: a partial `docker compose up` re-runs this one-shot init. A NEW genesis
    # would split the net in two (the ALPN embeds the genesis hash), so an existing one
    # is reused; `docker compose down -v` wipes it for a fresh net.
    if os.path.exists(os.path.join(shared, "ready")) and \
       os.path.exists(os.path.join(shared, "spec.json")):
        print("spec-init: %s/spec.json exists — reusing the running genesis "
              "(docker compose down -v for a fresh one)" % shared)
        return

    if "lasair" in clients:
        check_against_lasair(lasair, len(clients))

    host_of = (lambda i: host_ip) if host_ip else (lambda i: "%s.%d" % (ip_base, ip_start + i))
    vals, nodes = topology(clients, base, rpc_base, host_of)

    cfg_path = os.path.join(shared, "cfg.json")
    json.dump({"id": "jamswap-mixed", "genesis_validators": vals}, open(cfg_path, "w"))
    spec_path = os.path.join(shared, "spec.json")
    r = subprocess.run([polkajam, "gen-spec", cfg_path, spec_path], capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(spec_path):
        sys.exit("gen-spec failed: %s\n%s" % (r.stdout, r.stderr))

    topo = {"nodes": nodes, "bootnode": bootnode(nodes), "base_port": base, "rpc_base": rpc_base}
    json.dump(topo, open(os.path.join(shared, "nodes.json"), "w"), indent=2)
    for i, peers in peer_lists(nodes).items():
        open(os.path.join(shared, "peers_%d.txt" % i), "w").write(peers)
    for n in nodes:
        if n["role"] == "polkajam":
            with open(os.path.join(shared, n["seed"]), "wb") as f:
                f.write(devkeys.dev_seed(n["index"]))

    spec = json.load(open(spec_path))
    assert "genesis_header" in spec and "genesis_state" in spec, list(spec.keys())

    # Seed the jamswap service into the shared genesis (state only: the genesis header,
    # and so the ALPN, is unchanged). lasair's tool writes it; skipped with no SERVICE.
    service = os.environ.get("SERVICE", "")
    if service and os.path.exists(service):
        inject = [lasair, "--inject-service-spec", spec_path, "--service", service,
                  "--service-id", os.environ.get("SERVICE_ID", "100")]
        gbal = int(os.environ.get("GENESIS_BALANCE", "1000000"))     # display units/asset
        if gbal > 0:
            seed_path = os.path.join(shared, "genesis_accounts.json")
            json.dump(genesis_accounts(gbal), open(seed_path, "w"))
            inject += ["--seed-service-storage", seed_path]
            print("genesis accounts: %d dev accounts pre-registered, %s of each asset"
                  % (len(devkeys.PUBLISHED), gbal))
        r = subprocess.run(inject, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit("service injection failed: %s\n%s" % (r.stdout, r.stderr))
        print(r.stdout.strip())
        spec = json.load(open(spec_path))

    gh = bytes.fromhex(spec["genesis_header"])
    open(os.path.join(shared, "genesis_hex"), "w").write(
        hashlib.blake2b(gh, digest_size=32).hexdigest()[:8])
    open(os.path.join(shared, "ready"), "w").write("ok")
    print("genesis ready: %d validators (%s), %d state entries; bootnode %s"
          % (len(vals), ",".join(clients), len(spec["genesis_state"]), topo["bootnode"]))


if __name__ == "__main__":
    main()
