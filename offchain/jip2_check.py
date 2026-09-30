#!/usr/bin/env python3
"""Can jamswap run on this node? Probe a JAM node's JIP-2 RPC for what the DEX needs.

    python3 offchain/jip2_check.py ws://<node>:19800

The DEX reaches the chain only through JIP-2 (chain.Jip2Chain) and deploys its service at
runtime through the chain's Bootstrap service (deploy.py). This asks the node for every
method they call:

  - the reads, for real, at the node's best block (service 0 unless listServices names
    another); an answer of null ("nothing there") counts as served;
  - the two submissions, submitWorkPackage and submitPreimage, with no arguments, so
    nothing can reach the chain: any answer except JSON-RPC "method not found" (-32601)
    shows the node serves the method;
  - syncState and statistics, which only the dashboards and soak tooling read (optional).

It also reports whether a Bootstrap service (id 0) is there: runtime deploy needs one; a
net that seeds the DEX's service into genesis does not.

One line per method; exit 0 when every required method is served, 1 otherwise, 2 when the
node cannot be reached. Stdlib only (jip2.py).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jip2  # noqa: E402

NOT_FOUND = -32601
ZERO = jip2.b64(bytes(32))


def probe(rpc):
    """[(method, required, ok, detail)] for everything the DEX and deploy.py call."""
    rows = []

    def read(method, *params, required=True):
        try:
            r = rpc.call(method, *params)
            rows.append((method, required, True, "served" if r is not None else "served (null)"))
            return r
        except jip2.Jip2Error as e:
            detail = "not served" if e.code == NOT_FOUND else "error %s: %s" % (e.code, e.message)
            rows.append((method, required, False, detail))
            return None

    def write(method):
        try:
            rpc.call(method, retry=False)
            rows.append((method, True, True, "served (accepted an empty call)"))
        except jip2.Jip2Error as e:
            ok = e.code != NOT_FOUND
            rows.append((method, True, ok, "served (refused the empty probe, %s)" % e.code if ok
                         else "not served"))

    read("parameters")
    best = read("bestBlock")
    read("finalizedBlock")
    h = best.get("header_hash") if isinstance(best, dict) else None
    if h is None:
        for m in ("parent", "stateRoot", "listServices", "serviceData", "serviceValue",
                  "servicePreimage", "serviceRequest", "workPackageStatus"):
            rows.append((m, True, False, "not probed: bestBlock gave no header_hash"))
    else:
        read("parent", h)
        read("stateRoot", h)
        services = read("listServices", h)
        sid = services[0] if isinstance(services, list) and services else 0
        read("serviceData", h, sid)
        read("serviceValue", h, sid, ZERO)
        read("servicePreimage", h, sid, ZERO)
        read("serviceRequest", h, sid, ZERO, 0)
        read("workPackageStatus", h, ZERO, h)
    write("submitWorkPackage")
    write("submitPreimage")
    read("syncState", required=False)
    if h is not None:
        read("statistics", h, required=False)
    bootstrap = None
    if h is not None:
        try:
            bootstrap = rpc.call("serviceData", h, 0) is not None
        except jip2.Jip2Error:
            bootstrap = None
    return rows, bootstrap


def main(argv):
    if len(argv) != 2:
        print(__doc__.strip().split("\n\n")[1])
        return 2
    rpc = jip2.Jip2Client(argv[1], timeout=15)
    try:
        rows, bootstrap = probe(rpc)
    except OSError as e:
        print("cannot reach %s: %s" % (argv[1], e))
        return 2
    finally:
        rpc.close()
    width = max(len(m) for m, *_ in rows)
    for method, required, ok, detail in rows:
        mark = "ok  " if ok else ("MISS" if required else "--  ")
        print("%s  %-*s  %s%s" % (mark, width, method, detail, "" if required else "  (optional)"))
    print("Bootstrap service (id 0): %s" % {True: "present, runtime deploy possible",
                                            False: "absent: seed the service into genesis",
                                            None: "unknown"}[bootstrap])
    missing = [m for m, required, ok, _ in rows if required and not ok]
    print("jamswap can run on this node" if not missing else
          "jamswap needs: %s" % ", ".join(missing))
    return 0 if not missing else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
