#!/usr/bin/env python3
"""Report a jamswap net into the obs stack (monitor/obs, monitor/README.md).

    python3 nets/obsnet.py up NET              register the net's metrics endpoints under a
                                               fresh run id, annotate the start, print the link
    python3 nets/obsnet.py note NET TEXT [--tags a,b]
    python3 nets/obsnet.py down NET            annotate the end, unregister (before compose down:
                                               Prometheus leaves the net's network)
    python3 nets/obsnet.py link NET            the current run's dashboard links

./dex calls these; nets/soak.py imports note(). Optional-safe: when the stack is not
running, `up` prints one line saying so and everything else is silent; nothing here
ever fails the net command that called it.

Targets are the net's compose services that export Prometheus metrics: every lasair
node (:9615), the dex (:8080), the load generator (:9111), netwatch (:9106) and lasair's
builder bridge (:19980). PolkaJam, JavaJAM and pbnjam export none (PolkaJam's only
telemetry option is a JIP-3 push endpoint), so netwatch is their view: it reads every
node's JIP-2 RPC and exports one series per node.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import netgen  # noqa: E402
import profiles  # noqa: E402

OBS = os.path.join(REPO, "monitor", "obs", "obs")
LASAIR_METRICS_PORT = 9615           # lasair's mesh entrypoint, METRICS_PORT
SERVICES = {                         # compose service -> (job, client, port)
    "dex": ("dex", "dex", 8080),
    "loadgen": ("loadgen", "loadgen", 9111),
    "netwatch": ("netwatch", "netwatch", 9106),
    "builder": ("builder", "lasair", 19980),
    "canary": ("canary", "dex", 9110),
}


def obs(*args, timeout=30):
    try:
        return subprocess.run([sys.executable, OBS] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def available():
    return obs("ping", timeout=10).returncode == 0


def current(name):
    r = obs("current", name)
    return r.stdout.strip() if r.returncode == 0 else None


def targets(name, services):
    """{job: (client, ["node@host:port", ...])} for the given compose services of the net."""
    lasair = {n["service"] for n in netgen.nodes(name) if n["client"] == "lasair"}
    out = {}
    for s in services:
        if s in lasair:
            job, client, port = "lasair", "lasair", LASAIR_METRICS_PORT
        elif s in SERVICES:
            job, client, port = SERVICES[s]
        else:
            continue
        out.setdefault(job, (client, []))[1].append("%s@%s:%d" % (s, s, port))
    return out


def compose_services(name):
    try:
        r = subprocess.run(["docker", "compose", "-p", netgen.project(name), "-f",
                            netgen.compose_path(name), "config", "--services"], cwd=REPO,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return []
    return r.stdout.split()


def describe(name):
    p = netgen.profile(name)
    parts = ["validators %s" % p["clients"]]
    if "lasair" in p["clients"]:
        parts.append("lasair %s" % os.environ.get("LASAIR_IMAGE", profiles.LASAIR_IMAGE))
    if "pj" in p["clients"]:
        parts.append("PolkaJam %s" % os.environ.get("PJ_RELEASE", profiles.PJ_RELEASE))
    return "; ".join(parts)


def register(name, quiet=False):
    """Register the net under a fresh run id; the run id, or None if obs is not running."""
    if not available():
        if not quiet:
            print("obs: not running, so this run is not in Grafana (start it: monitor/obs/obs up)")
        return None
    run_id = obs("new-run", name).stdout.strip()
    for job, (client, tl) in sorted(targets(name, compose_services(name)).items()):
        r = obs("register", name, run_id, job, *tl, "--label", "client=" + client,
                "--project", netgen.project(name))
        if r.returncode:
            print("obs: register %s failed: %s" % (job, r.stderr.strip()))
    obs("annotate", run_id, "%s up: %s" % (name, describe(name)), "--tags", "up")
    print("obs: run %s -> %s" % (run_id, obs("link", run_id).stdout.strip()))
    return run_id


def ensure(name):
    """The net's current run id, registering the net first if obs runs but it is not in."""
    if not available():
        return None
    return current(name) or register(name, quiet=True)


def note(name, text, tags="", start=None, end=None):
    """Annotate the net's current run; silent no-op without obs or a registered run."""
    run_id = current(name)
    if not run_id or not available():
        return
    args = ["annotate", run_id, text, "--tags", tags]
    if start:
        args += ["--start", str(start)]
    if end:
        args += ["--end", str(end)]
    obs(*args)


def down(name):
    run_id = current(name)
    if run_id and available():
        obs("annotate", run_id, "%s down" % name, "--tags", "down")
    # always: a Prometheus still attached to the net's network would block compose down
    obs("unregister", name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["up", "note", "down", "link"])
    ap.add_argument("net")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--tags", default="")
    a = ap.parse_args(argv)
    netgen.profile(a.net)
    if a.cmd == "up":
        register(a.net)
    elif a.cmd == "note":
        note(a.net, a.text or "", a.tags)
    elif a.cmd == "down":
        down(a.net)
    elif a.cmd == "link":
        run_id = current(a.net)
        if not run_id:
            print("obs: %s has no registered run" % a.net)
            return 1
        print(obs("link", run_id, "--all").stdout, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
