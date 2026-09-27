#!/usr/bin/env python3
"""Generate a test net's compose file from its per-index client layout.

    python3 nets/netgen.py list               the profiles
    python3 nets/netgen.py write [NAME ...]   (re)write nets/compose/<NAME>.yml (all by default)
    python3 nets/netgen.py check              exit 1 if a committed file is stale
    python3 nets/netgen.py nodes NAME         the node table (JSON): what runs where
    python3 nets/netgen.py env NAME           shell assignments ./dex evaluates

One adapter per client says how to run it as validator i on the shared genesis
(nets/genesis.py mints it in `spec-init`):

  client   image (pinned)                           started as
  lasair   ${LASAIR_IMAGE}                          mesh-entrypoint OWN=i (dev account i)
  pj       jamswap-polkajam (built: the release     pj-entrypoint: --peer-id + dev seed i
           tarball fetched + sha256-checked)
  pbnjam   shimonchick/pbnjam-node@sha256           --chain spec --dev-validator i
  javajam  native (release zip + JDK 25, host)      --chain spec --dev-validator i
           or ghcr.io/methodfive/javajam@sha256     (Linux; heap capped by JAVAJAM_HEAP)

Addressing. Validator i has a static IP 10.231.<net>.(10+i) and its JAMNP-S port
41000+100*net+i; that pair is baked into genesis. When a node runs natively on the
host (JavaJAM on macOS, whose Docker images SIGILL there), no container IP is
reachable from it, so the net is minted with HOST_IP set: every validator's genesis
address is HOST_IP:port and every container publishes its port on HOST_IP. Traffic
between containers then hairpins through the host; the same file serves both modes.
"""
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import genesis  # noqa: E402
import profiles  # noqa: E402

REPO = os.path.dirname(HERE)
OUT_DIR = os.path.join(HERE, "compose")
SERVICE_PREFIX = {"lasair": "lm", "polkajam": "pj", "pbnjam": "pb", "javajam": "jj"}
CTX = "../.."                        # the repo root, relative to nets/compose/


def profile(name):
    if name not in profiles.PROFILES:
        sys.exit("unknown net %r; known: %s" % (name, ", ".join(profiles.PROFILES)))
    return profiles.PROFILES[name]


def generated(name):
    return "compose" not in profile(name)


def compose_path(name):
    p = profile(name)
    return os.path.join(REPO, p["compose"]) if "compose" in p else \
        os.path.join(OUT_DIR, name + ".yml")


def project(name):
    return profile(name).get("project", name)


def ports(p):
    net = p["net"]
    return 41000 + 100 * net, 42000 + 100 * net


def nodes(name):
    """Every validator of the net: index, client, compose service, ports, address."""
    p = profile(name)
    clients = genesis.parse_clients(p["clients"])
    out = []
    for i, c in enumerate(clients):
        n = {"index": i, "client": c, "service": "%s%d" % (SERVICE_PREFIX[c], i)}
        if generated(name):
            quic, rpc = ports(p)
            n.update(port=quic + i, rpc=None if c == "lasair" else rpc + i,
                     ip="10.231.%d.%d" % (p["net"], 10 + i))
        out.append(n)
    return out


def dev_all_keys_default(name):
    """Key sharing (lasair's pre-#54 devnet mode) is ON only where lasair runs."""
    return "lasair" in genesis.parse_clients(profile(name)["clients"])


# ---- adapters: one compose service per validator ---------------------------------
def _pj_image(p, with_lasair):
    build = {"context": CTX, "dockerfile": "mixed/Dockerfile.polkajam",
             "target": "with-lasair" if with_lasair else "polkajam",
             "args": {"PJ_RELEASE": "${PJ_RELEASE:-%s}" % profiles.PJ_RELEASE}}
    if with_lasair:
        build["args"]["MESH_IMAGE"] = "${LASAIR_IMAGE:-%s}" % profiles.LASAIR_IMAGE
        return build, "jamswap-polkajam:${PJ_RELEASE:-%s}-lasair" % profiles.PJ_RELEASE
    return build, "jamswap-polkajam:${PJ_RELEASE:-%s}" % profiles.PJ_RELEASE


def _node_common(n):
    return {"depends_on": {"spec-init": {"condition": "service_completed_successfully"}},
            "networks": {"net": {"ipv4_address": n["ip"]}},
            "restart": "unless-stopped"}


def _udp(n):
    return "${HOST_IP:-127.0.0.1}:%d:%d/udp" % (n["port"], n["port"])


def _rpc(n):
    return "127.0.0.1:%d:%d" % (n["rpc"], n["rpc"])


def svc_polkajam(n, p, ctx):
    # the image spec-init builds (one build per net, not one per node)
    s = {"image": ctx["pj_image"], "pull_policy": "never"}
    s.update(_node_common(n))
    s["environment"] = {"ROLE": "validator", "INDEX": str(n["index"]), "SHARED": "/shared",
                        "FINALITY_MODE": p["finality"], "EXTERNAL_IP": "${HOST_IP:-}"}
    s["volumes"] = ["shared:/shared"]
    s["ports"] = [_udp(n), _rpc(n)]
    return s


def svc_pbnjam(n, p, ctx):
    # PolkaJam-style CLI (its Docker Hub usage + the image's default command):
    # --dev-validator i takes validator i's key, and its address from genesis.
    s = {"image": "${PBNJAM_IMAGE:-%s}" % profiles.PBNJAM_IMAGE}
    s.update(_node_common(n))
    s["command"] = ["--chain", "/shared/spec.json", "--dev-validator", str(n["index"]),
                    "--rpc-port", str(n["rpc"]), "--temp"]
    s["volumes"] = ["shared:/shared:ro"]
    s["ports"] = [_udp(n), _rpc(n)]
    return s


def svc_javajam(n, p, ctx):
    # Linux hosts only (profile javajam-docker): the images SIGILL under Docker Desktop
    # on Apple silicon, where ./dex runs JavaJAM natively instead (nets/javajam-native.sh).
    # The image pins a 12 GB heap (-Xms12g -Xmx12g -XX:+AlwaysPreTouch); this keeps its
    # other JVM flags and caps the heap.
    s = {"image": "${JAVAJAM_IMAGE:-%s}" % profiles.JAVAJAM_IMAGES["amd64"],
         "profiles": ["javajam-docker"]}
    s.update(_node_common(n))
    s["environment"] = {"JAVA_TOOL_OPTIONS": (
        "-Xms512m -Xmx${JAVAJAM_HEAP:-2g} -XX:+UseSignalChaining -XX:+UseFastJNIAccessors "
        "-XX:+UseThreadPriorities -XX:+UseZGC --enable-native-access=ALL-UNNAMED "
        "-Djava.library.path=/app")}
    s["command"] = ["run", "--chain", "/shared/spec.json", "--dev-validator", str(n["index"]),
                    "--listen-ip", "0.0.0.0", "--port", str(n["port"]),
                    "--rpc-listen-ip", "0.0.0.0", "--rpc-port", str(n["rpc"]),
                    "--finality-mode", p["finality"], "--data-path", "/data"]
    s["volumes"] = ["shared:/shared:ro"]
    s["ports"] = [_udp(n), _rpc(n)]
    return s


def svc_lasair(n, p, ctx):
    s = {"image": "${LASAIR_IMAGE:-%s}" % profiles.LASAIR_IMAGE}
    s.update(_node_common(n))
    env = {"SPEC": "/shared/spec.json", "OWN": str(n["index"]), "IDENTITY": str(100 + n["index"]),
           "PORT": str(n["port"]), "INTERVAL": "1",
           # chain-paced when every validator is lasair; wall-clock next to PolkaJam/JavaJAM
           "WALL": "0" if ctx["all_lasair"] else "1",
           "LASAIR_FINALITY": "${LASAIR_FINALITY:-grandpa}",
           # key sharing (docs/NETS.md): unset = sign guarantees as every lasair index;
           # LASAIR_GUARANTOR_OWN= (empty; ./dex sets it for LASAIR_DEV_ALL_KEYS=0) = own only
           "GUARANTOR_OWN": "${LASAIR_GUARANTOR_OWN-%s}" % ctx["lasair_set"],
           "LASAIR_DEV_ALL_KEYS": "${LASAIR_DEV_ALL_KEYS:-1}"}
    if p.get("dex"):
        env.update(SERVICE="/work/jamswap-service.jam", SERVICE_ID="100")
    s["environment"] = env
    s["volumes"] = ["shared:/shared", "%s/service/jamswap-service.jam:/work/jamswap-service.jam:ro" % CTX]
    s["ports"] = [_udp(n)]
    return s


ADAPTERS = {"polkajam": svc_polkajam, "pbnjam": svc_pbnjam, "javajam": svc_javajam,
            "lasair": svc_lasair}


def _bridge(binary, env):
    return {"image": "${LASAIR_IMAGE:-%s}" % profiles.LASAIR_IMAGE,
            "depends_on": {"spec-init": {"condition": "service_completed_successfully"}},
            "volumes": ["shared:/shared"], "entrypoint": ["bash", "-c"],
            "command": ["for _ in $$(seq 1 60); do [ -s /shared/genesis_hex ] && break; sleep 1; done\n"
                        "export LASAIR_JAMNP_GENESIS_HEX=$$(cat /shared/genesis_hex)\n"
                        "exec /usr/local/bin/%s\n" % binary],
            "environment": env, "networks": {"net": {}}, "restart": "unless-stopped"}


def dex_backend(name):
    """How a net's DEX reaches the chain (None: the net has no DEX).

    jamnp  the layout has a lasair node: lasair's CE-133 builder and CE-129 reader
           bridges to it, the service seeded into genesis (lasair has no JIP-2 server or
           Bootstrap service yet: lasair#68, #69);
    jip2   otherwise: one node's JIP-2 RPC, the service deployed at startup through the
           chain's Bootstrap service (offchain/deploy.py), no lasair image anywhere."""
    p = profile(name)
    if not p.get("dex"):
        return None
    return "jamnp" if "lasair" in genesis.parse_clients(p["clients"]) else "jip2"


def dex_url(p):
    return "http://localhost:%d" % (8200 + p["net"])


def netwatch_port(p):
    return 9300 + p["net"]


def dex_services_jamnp(p, ns):
    """builder -> every lasair node's CE-133 endpoint (its port + 1); reader -> the
    first lasair node over CE-129; dex on :8200+net. lasair's bridges: see #10-#12."""
    lm = [n for n in ns if n["client"] == "lasair"]
    first = lm[0]
    return {
        "builder": _bridge("jamnp-builder", {
            "LASAIR_GUARANTOR_HOST": ",".join(n["ip"] for n in lm),
            "LASAIR_GUARANTOR_PORT": ",".join(str(n["port"] + 1) for n in lm),
            "LASAIR_NODE_RPC": "", "LASAIR_BUILDER_HTTP_PORT": "19980"}),
        "reader": _bridge("lasair-reader", {
            "LASAIR_NODE_HOST": first["ip"], "LASAIR_NODE_PORT": str(first["port"]),
            "LASAIR_READER_HTTP_PORT": "19990"}),
        "dex": {
            "build": {"context": CTX, "dockerfile": "offchain/Dockerfile"},
            "depends_on": ["builder", "reader"],
            "environment": {"SERVICE_ID": "100", "BUILDER_URL": "http://builder:19980",
                            "READER_URL": "http://reader:19990",
                            "NODE_METRICS_URL": "http://%s:9615/metrics" % first["service"],
                            "PORT": "8080", "PYTHONUNBUFFERED": "1",
                            "ORDER_EVENTS_FILE": "/shared/order_events.jsonl"},
            "volumes": ["%s/offchain:/app:ro" % CTX,
                        "%s/service/jamswap-service.jam:/work/jamswap-service.jam:ro" % CTX,
                        "shared:/shared"],
            "working_dir": "/app", "command": ["python3", "server.py"],
            "ports": ["%d:8080" % (8200 + p["net"])],
            "networks": {"net": {}}, "restart": "unless-stopped"},
    }


def _jip2_url(n):
    # a node's JIP-2 RPC as the net's containers reach it: its service on the net. A
    # JavaJAM node running natively (macOS) serves it on the host's loopback instead,
    # which containers reach at Docker Desktop's host.docker.internal: ./dex sets
    # JAVAJAM_RPC_HOST to that for the native runner, and leaves it unset for the Docker
    # runner (the node's own service then).
    if n["client"] == "javajam":
        return "ws://${JAVAJAM_RPC_HOST:-%s}:%d" % (n["service"], n["rpc"])
    return "ws://%s:%d" % (n["service"], n["rpc"])


def gateway(p):
    """The DEX's own node: an ordinary PolkaJam node (no validator key) that joins the
    net, JAMNP-S on the net's port block + 50, JIP-2 RPC on its RPC block + 50. A
    validator's RPC answers submitWorkPackage with "Failed to submit work-package to even
    a single proxy/guarantor" (PolkaJam nightly-2026-09-22, every pj6 validator); an
    ordinary node forwards the package to the core's guarantors (docs/NETS.md)."""
    quic, rpc = ports(p)
    return {"service": "rpc", "client": "polkajam", "port": quic + 50, "rpc": rpc + 50}


# clients whose validator RPC takes work-packages (JIP-2 submitWorkPackage) and passes them
# to the core's guarantors. Observed black box (jamswap#18): JavaJAM 0.4.3 validators do;
# PolkaJam's (nightly-2026-09-22) answer "Failed to submit work-package to even a single
# proxy/guarantor", hence the gateway node.
VALIDATOR_SUBMITS = ("javajam",)


def submission_nodes(p, ns):
    """NAME=URL of every node the DEX submits through, one package each in turn: its
    gateway, then the first validator of each client in VALIDATOR_SUBMITS, so that
    client's submission path carries DEX rounds too."""
    gw = gateway(p)
    out = ["%s=ws://%s:%d" % (gw["service"], gw["service"], gw["rpc"])]
    for c in VALIDATOR_SUBMITS:
        first = next((n for n in ns if n["client"] == c and n["rpc"]), None)
        if first:
            out.append("%s=%s" % (first["service"], _jip2_url(first)))
    return out


def dex_services_jip2(p, ns, ctx):
    """The DEX on its gateway node's JIP-2 RPC, with no SERVICE_ID: at startup the dex
    deploys the service through the Bootstrap service and lists the markets and funds
    the dev accounts with ordinary work-items (offchain/deploy.py, dex_setup.py), the
    authorizer taken from the net's spec.json. Beside it the load generator (./dex load)
    and netwatch over every validator (A1-A3; `./dex soak` drives both). Where a client's
    validators take work-packages themselves, the dex also submits through one of them,
    in turn with the gateway (CHAIN_SUBMIT_RPC; submission_nodes)."""
    rpc_nodes = [n for n in ns if n["rpc"]]
    gw = gateway(p)
    via = submission_nodes(p, ns)
    offchain = "%s/offchain:/app:ro" % CTX
    blob = "%s/service/jamswap-service.jam:/work/jamswap-service.jam:ro" % CTX
    dex_build = {"context": CTX, "dockerfile": "offchain/Dockerfile"}
    node = {"image": ctx["pj_image"], "pull_policy": "never",
            "depends_on": {"spec-init": {"condition": "service_completed_successfully"}},
            "environment": {"ROLE": "node", "SHARED": "/shared", "PORT": str(gw["port"]),
                            "RPC_PORT": str(gw["rpc"]), "FINALITY_MODE": p["finality"],
                            "EXTERNAL_IP": "${HOST_IP:-}"},
            "volumes": ["shared:/shared"],
            "ports": [_udp(gw), _rpc(gw)],
            "networks": {"net": {}}, "restart": "unless-stopped"}
    dex_env = {"CHAIN_BACKEND": "jip2", "CHAIN_RPC": _jip2_url(gw)}
    if len(via) > 1:
        dex_env["CHAIN_SUBMIT_RPC"] = " ".join(via)
    return {
        gw["service"]: node,
        "dex": {
            "build": dex_build,
            "depends_on": {"spec-init": {"condition": "service_completed_successfully"},
                           gw["service"]: {"condition": "service_started"}},
            "environment": dict(dex_env, **{
                            "CHAIN_SPEC": "/shared/spec.json",
                            "SERVICE_CODE": "/work/jamswap-service.jam",
                            "DEPLOY_STATE": "/shared/jamswap_deploy.json",
                            # the footprint is readable here and grows with use: keep the
                            # JAMKB reserve at its target, or backpressure refuses every
                            # order after ~15 min (server.py, reserve keeper)
                            "RESERVE_TOPUP": "1",
                            "PORT": "8080", "PYTHONUNBUFFERED": "1",
                            "ORDER_EVENTS_FILE": "/shared/order_events.jsonl"}),
            "volumes": [offchain, blob, "shared:/shared"],
            "working_dir": "/app", "command": ["python3", "server.py"],
            "ports": ["%d:8080" % (8200 + p["net"])],
            "networks": {"net": {}}, "restart": "unless-stopped"},
        "loadgen": {
            "build": dex_build,
            "depends_on": ["dex"],
            "command": ["python3", "loadgen.py"],
            "environment": {"DEX_URL": "http://dex:8080", "PROFILE": "${PROFILE:-trading}",
                            "RATE": "${RATE:-12}", "SEALED_RATIO": "${SEALED_RATIO:-0.2}",
                            "PYTHONUNBUFFERED": "1"},
            "volumes": [offchain],
            "networks": {"net": {}}, "restart": "unless-stopped"},
        "netwatch": {
            "build": {"context": CTX, "dockerfile": "monitor/Dockerfile"},
            "depends_on": {"spec-init": {"condition": "service_completed_successfully"}},
            "environment": {
                "NETWATCH_NODES": " ".join("%s,%s,%s" % (n["service"], n["client"], _jip2_url(n))
                                           for n in rpc_nodes),
                "NETWATCH_VALIDATORS": ",".join(n["service"] for n in ns)},
            "ports": ["127.0.0.1:%d:9106" % netwatch_port(p)],
            "networks": {"net": {}}, "restart": "unless-stopped"},
    }


def compose(name):
    p = profile(name)
    if not generated(name):
        raise ValueError("%s is a hand-written compose file (%s)" % (name, p["compose"]))
    ns = nodes(name)
    clients = [n["client"] for n in ns]
    with_lasair = "lasair" in clients
    backend = dex_backend(name)
    pj_build, pj_image = _pj_image(p, with_lasair)
    quic, rpc = ports(p)
    ctx = {"pj_build": pj_build, "pj_image": pj_image,
           "all_lasair": all(c == "lasair" for c in clients),
           "lasair_set": ",".join(str(n["index"]) for n in ns if n["client"] == "lasair")}

    init_env = {"ROLE": "init", "SHARED": "/shared", "CLIENTS": p["clients"],
                "IP_BASE": "10.231.%d" % p["net"], "IP_START": "10",
                "BASE_PORT": str(quic), "RPC_BASE": str(rpc), "HOST_IP": "${HOST_IP:-}"}
    init = {"build": pj_build, "image": pj_image, "restart": "no",
            "volumes": ["shared:/shared"], "networks": {"net": {}}}
    if backend == "jamnp":                    # lasair writes the service into genesis
        init_env.update(SERVICE="/work/jamswap-service.jam", SERVICE_ID="100")
        init["volumes"].append("%s/service/jamswap-service.jam:/work/jamswap-service.jam:ro" % CTX)
    else:
        init_env["GENESIS_BALANCE"] = "0"
    init["environment"] = init_env

    services = {"spec-init": init}
    for n in ns:
        services[n["service"]] = ADAPTERS[n["client"]](n, p, ctx)
    if backend == "jamnp":
        services.update(dex_services_jamnp(p, ns))
    elif backend == "jip2":
        services.update(dex_services_jip2(p, ns, ctx))
    return {"services": services,
            "networks": {"net": {"driver": "bridge",
                                 "ipam": {"config": [{"subnet": "10.231.%d.0/24" % p["net"]}]}}},
            "volumes": {"shared": {}}}


def header(name):
    p = profile(name)
    ns = nodes(name)
    lines = [
        "GENERATED by nets/netgen.py from profile %r (%s) - do not edit." % (name, p["issue"]),
        "Change nets/profiles.py or nets/netgen.py, then `./dex gen`.",
        "",
        p["about"] + ".",
        "",
        "  ./dex up NET=%s        start it (JavaJAM runs natively on macOS)" % name,
        "  ./dex heads NET=%s     one head + finality across every node's RPC" % name,
        "  ./dex down NET=%s      tear down, wipe the chain" % name,
    ]
    if dex_backend(name) == "jip2":
        gw = gateway(p)
        lines += [
            "  ./dex load NET=%s      start the load generator (up leaves it stopped)" % name,
            "  ./dex soak NET=%s 600  A1-A4: load + netwatch, then parity + soak verdict" % name,
            "",
            "DEX %s (CHAIN_BACKEND=jip2, no SERVICE_ID) on the JIP-2 RPC of `rpc`," % dex_url(p),
            "an ordinary PolkaJam node (udp %d, RPC 127.0.0.1:%d): at startup it deploys" % (
                gw["port"], gw["rpc"]),
            "the service through the Bootstrap service, lists the markets and funds the",
            "dev accounts. netwatch over every validator: 127.0.0.1:%d (/metrics, /verdict)."
            % netwatch_port(p),
        ]
        via = [v.split("=", 1)[0] for v in submission_nodes(p, ns)]
        if len(via) > 1:
            lines += [
                "Work-packages go through %s in turn (CHAIN_SUBMIT_RPC): %s's validator RPC"
                % (", ".join(via[:-1]) + " and " + via[-1], " and ".join(via[1:])),
                "takes them itself, where a PolkaJam validator's does not.",
            ]
    lines += [
        "",
        "  i  client    service  JAMNP-S udp  RPC (host 127.0.0.1)",
    ]
    for n in ns:
        where = "  (native on macOS)" if n["client"] == "javajam" else ""
        lines.append("  %d  %-8s  %-7s  %-11d  %s%s" % (
            n["index"], n["client"], n["service"], n["port"], n["rpc"] or "-  (logs)", where))
    lines += [
        "",
        "Keys: validator i is standard dev account i and only its own node signs as it,",
    ]
    if dev_all_keys_default(name):
        lines += [
            "EXCEPT lasair (LASAIR_DEV_ALL_KEYS=1 by default until lasair#62): every lasair",
            "node signs guarantees as any lasair index and, with lasair <= 2.x, assures for",
            "every dev account. LASAIR_DEV_ALL_KEYS=0 narrows guarantees to its own index.",
        ]
    else:
        lines.append("with no exception: no lasair node, no shared keys.")
    return "".join("# %s\n" % l if l else "#\n" for l in lines)


# ---- a small YAML writer (stdlib only; compose reads it) -------------------------
def _key(k):
    ok = all(ch.isalnum() or ch in "_-." for ch in k)
    return k if ok else json.dumps(k)


def _scalar(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int):
        return str(v)
    return json.dumps(v)


def _emit(v, indent, out):
    pad = "  " * indent
    if isinstance(v, dict):
        for k, x in v.items():
            if isinstance(x, (dict, list)) and x:
                out.append("%s%s:" % (pad, _key(k)))
                _emit(x, indent + 1, out)
            elif isinstance(x, dict):
                out.append("%s%s: {}" % (pad, _key(k)))
            elif isinstance(x, list):
                out.append("%s%s: []" % (pad, _key(k)))
            elif isinstance(x, str) and "\n" in x:
                out.append("%s%s: |" % (pad, _key(k)))
                out.extend(pad + "  " + l for l in x.rstrip("\n").split("\n"))
            else:
                out.append("%s%s: %s" % (pad, _key(k), _scalar(x)))
    else:
        for x in v:
            if isinstance(x, str) and "\n" in x:
                out.append("%s- |" % pad)
                out.extend(pad + "  " + l for l in x.rstrip("\n").split("\n"))
            elif isinstance(x, dict) and x:
                sub = []
                _emit(x, indent + 1, sub)           # "- " + the first key, the rest aligned
                sub[0] = pad + "- " + sub[0].lstrip()
                out.extend(sub)
            else:
                out.append("%s- %s" % (pad, _scalar(x)))


def render(name):
    out = []
    _emit(compose(name), 0, out)
    return header(name) + "\n" + "\n".join(out) + "\n"


# ---- CLI -------------------------------------------------------------------------
def _sh(v):
    return "'" + str(v).replace("'", "'\\''") + "'"


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    gen = [n for n in profiles.PROFILES if generated(n)]
    if cmd == "list":
        for n, p in profiles.PROFILES.items():
            print("%-18s %-5s %-38s %s" % (n, p["issue"], p["clients"], p["about"]))
    elif cmd == "write":
        os.makedirs(OUT_DIR, exist_ok=True)
        for n in argv[2:] or gen:
            if generated(n):
                open(compose_path(n), "w").write(render(n))
    elif cmd == "check":
        stale = [n for n in gen if not os.path.exists(compose_path(n))
                 or open(compose_path(n)).read() != render(n)]
        if stale:
            sys.exit("stale generated compose files: %s (run ./dex gen)" % ", ".join(stale))
    elif cmd == "nodes":
        print(json.dumps(nodes(argv[2]), indent=2))
    elif cmd == "env":
        name = argv[2]
        ns = nodes(name)
        p = profile(name)
        print("NET_FILE=%s" % _sh(os.path.relpath(compose_path(name), REPO)))
        print("NET_PROJECT=%s" % _sh(project(name)))
        print("NET_GENERATED=%d" % generated(name))
        print("NET_CLIENTS=%s" % _sh(",".join(n["client"] for n in ns)))
        print("NET_LASAIR_SET=%s" % _sh(",".join(str(n["index"]) for n in ns if n["client"] == "lasair")))
        print("NET_DEV_ALL_KEYS_DEFAULT=%d" % dev_all_keys_default(name))
        print("NET_DEX_URL=%s" % _sh(
            dex_url(p) if generated(name) and p.get("dex") else
            {"lasair6": "http://localhost:8081", "mixed": "http://localhost:8090"}.get(name, "")))
        # generated nets only: jip2 = the DEX with its loadgen and netwatch (./dex load, soak)
        print("NET_DEX_BACKEND=%s" % _sh(dex_backend(name) or "" if generated(name) else ""))
        print("NET_FINALITY=%s" % _sh(p.get("finality", "")))
        print("NET_JAVAJAM=%s" % _sh(" ".join("%d:%d:%d" % (n["index"], n["port"], n["rpc"])
                                              for n in ns if n["client"] == "javajam")))
        # the other validators' compose services (./dex starts them before anything else)
        print("NET_OTHER_VALIDATORS=%s" % _sh(" ".join(n["service"] for n in ns
                                                       if n["client"] != "javajam")))
        print("JAVAJAM_IMAGES=%s" % _sh(" ".join("%s=%s" % kv for kv in profiles.JAVAJAM_IMAGES.items())))
    else:
        sys.exit(__doc__.split("\n\n")[1])


if __name__ == "__main__":
    main(sys.argv)
