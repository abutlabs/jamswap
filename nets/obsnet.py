#!/usr/bin/env python3
"""Report a jamswap net into the observability stack (the abutlabs/observability repo).

    python3 nets/obsnet.py begin NET     shell assignments for ./dex: OBS_RUN_ID, the run id
                                         for the net's container labels, and OBS_JIP3, the
                                         JIP-3 endpoint its PolkaJam nodes report to. A
                                         running net keeps both (a re-up recreates nothing);
                                         a new one gets a fresh run id and the stack's
                                         receiver; without the stack both are empty
    python3 nets/obsnet.py up NET        annotate the start and print the dashboard links
    python3 nets/obsnet.py note NET TEXT [--tags a,b]
    python3 nets/obsnet.py down NET      annotate the end and end the run
    python3 nets/obsnet.py link NET      the current run's dashboard links

./dex calls these; nets/soak.py imports them. Optional-safe: without the stack every
call prints at most one line and never fails the net command that called it.

How a net gets in: its compose services carry org.abutlabs.obs.* labels (nets/netgen.py
writes them into the generated compose files; the hand-written ones carry them too).
The stack's Grafana Alloy discovers every labelled container and scrapes the ones with a
metrics port (lasair nodes :9615, dex :8080, loadgen :9111, netwatch :9106, builder
:19980), ships every labelled container's logs to Loki, and its JIP-2 exporter polls the
nodes whose RPC is labelled. `./dex up` passes the run id to the labels through
OBS_RUN_ID. The stack lives at $OBS_HOME, default ../observability beside this checkout.
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

OBS_HOME = os.environ.get("OBS_HOME") or os.path.join(os.path.dirname(REPO), "observability")
OBS = os.path.join(OBS_HOME, "obs")
RUN_LABEL = "org.abutlabs.obs.run_id"
JIP3 = "obs-jip3:9910"                   # the receiver's alias on every observed network


def obs(*args, timeout=30, stdin=None):
    """Run the obs CLI; a CompletedProcess (rc 127 when the CLI is not there)."""
    if not os.path.exists(OBS):
        return subprocess.CompletedProcess(args, 127, "", "no obs CLI at %s" % OBS)
    try:
        return subprocess.run([sys.executable, OBS] + list(args), input=stdin, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        return subprocess.CompletedProcess(args, 1, "", str(e))


def available():
    return obs("ping", timeout=10).returncode == 0


def _docker(*args):
    try:
        return subprocess.run(["docker"] + list(args), stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, text=True, timeout=30).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def container_run_id(name):
    """The run id the net's running containers carry, or None."""
    out = _docker("ps", "--filter", "label=com.docker.compose.project=%s" % netgen.project(name),
                  "--format", '{{.Label "%s"}}' % RUN_LABEL)
    ids = [x for x in out.split() if x]
    return ids[0] if ids else None


def container_telemetry(name):
    """The TELEMETRY endpoint the net's running nodes were started with ('' if none)."""
    ids = _docker("ps", "-q", "--filter", "label=com.docker.compose.project=%s" % netgen.project(name)).split()
    if not ids:
        return ""
    for line in _docker("inspect", "-f", "{{range .Config.Env}}{{println .}}{{end}}", *ids).splitlines():
        if line.startswith("TELEMETRY="):
            return line.split("=", 1)[1]
    return ""


def current(name):
    """The net's run id: what its containers carry, else what the stack recorded."""
    rid = container_run_id(name)
    if rid:
        return rid
    r = obs("current", name)
    return r.stdout.strip() if r.returncode == 0 else None


def run_id(name):
    """The run id the net's series carry: current(), else the collectors' default."""
    return current(name) or "%s-adhoc" % name


def begin(name):
    """(run id, JIP-3 endpoint) for the net's containers. A net that is already running
    keeps both (a changed label or environment would make compose recreate its
    containers). Both empty without the stack: the net runs as before."""
    rid = container_run_id(name)
    if rid:
        return rid, container_telemetry(name)
    if not available():
        return "", ""
    r = obs("begin", name, "--meta", "describe=" + describe(name))
    rid = r.stdout.strip() if r.returncode == 0 else ""
    return rid, JIP3 if rid else ""


def describe(name):
    p = netgen.profile(name)
    parts = ["validators %s" % p["clients"]]
    if "lasair" in p["clients"]:
        parts.append("lasair %s" % os.environ.get("LASAIR_IMAGE", profiles.LASAIR_IMAGE))
    if "pj" in p["clients"]:
        parts.append("PolkaJam %s" % os.environ.get("PJ_RELEASE", profiles.PJ_RELEASE))
    return "; ".join(parts)


def links(rid):
    """[(dashboard, url)] for every run-scoped dashboard, or [] without the CLI."""
    r = obs("link", rid, "--all")
    out = []
    for line in r.stdout.splitlines() if r.returncode == 0 else []:
        name, _, u = line.partition(" http")
        if u:
            out.append((name.strip(), "http" + u.strip()))
    return out


def up(name):
    """After compose up: annotate the start and print the links (one line without the stack)."""
    if not os.path.exists(OBS):
        print("obs: no observability stack at %s (set OBS_HOME): this run is not in Grafana" % OBS_HOME)
        return None
    if not available():
        print("obs: the stack is not running (%s up): this run is not in Grafana" % OBS)
        return None
    rid = current(name)
    if not rid:
        print("obs: %s runs without a run id (it was started before the stack): its series "
              "carry run_id=%s-adhoc" % (name, name))
        return None
    obs("annotate", rid, "%s up: %s" % (name, describe(name)), "--tags", "up")
    print("obs: run %s" % rid)
    for dash, u in links(rid):
        if dash.endswith(("Chain health", "DEX", "Soak runs")):
            print("  %-24s %s" % (dash, u))
    return rid


def note(name, text, tags="", start=None, end=None):
    """Annotate the net's current run; silent no-op without the stack or a run."""
    rid = current(name)
    if not rid or not available():
        return
    args = ["annotate", rid, text, "--tags", tags]
    if start:
        args += ["--start", str(start)]
    if end:
        args += ["--end", str(end)]
    obs(*args)


def push(name, rid, text, job="soak"):
    """Push Prometheus text to the stack's Pushgateway, grouped by run and net. True if it
    landed; never raises."""
    r = obs("push", job, "--group", "run_id=" + rid, "--group", "net=" + name, stdin=text)
    return r.returncode == 0


def down(name):
    rid = current(name)
    if rid and available():
        obs("annotate", rid, "%s down" % name, "--tags", "down")
    obs("end", name)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("cmd", choices=["begin", "up", "note", "down", "link"])
    ap.add_argument("net")
    ap.add_argument("text", nargs="?")
    ap.add_argument("--tags", default="")
    a = ap.parse_args(argv)
    netgen.profile(a.net)
    if a.cmd == "begin":
        rid, jip3 = begin(a.net)
        print("OBS_RUN_ID=%s\nOBS_JIP3_DEFAULT=%s" % (rid, jip3))
    elif a.cmd == "up":
        up(a.net)
    elif a.cmd == "note":
        note(a.net, a.text or "", a.tags)
    elif a.cmd == "down":
        down(a.net)
    elif a.cmd == "link":
        rid = current(a.net)
        if not rid:
            print("obs: %s has no run" % a.net)
            return 1
        for dash, u in links(rid) or [("", "(no obs CLI at %s)" % OBS)]:
            print("%-26s %s" % (dash, u))
    return 0


if __name__ == "__main__":
    sys.exit(main())
