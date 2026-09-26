"""Finality soak (Gate 3a): a sample every 5 min for ~13 h, then the verdict. A thin wrapper
over netwatch.py (issue #15), which replaced this script's lasair-only loop: it watches every
node, not one, lines their heads up by hash where they serve JIP-2, and judges one head,
liveness and finality advance (conflicts, regressions, stalls) at the end.

Nodes come from NETWATCH_NODES, else from the DEX's own chain env (it runs beside the dex:
CHAIN_BACKEND=jip2 with CHAIN_RPC, or jamnp with NODE_METRICS_URL). Extra arguments go to
`netwatch.py poll`, e.g. --samples /shared/gate3a.jsonl --require-finality; the DEX's
volume and settle reverts are the order verdict's (soak_verdict.py, which takes the
samples with --chain)."""
import sys

import netwatch


def main(argv=None):
    extra = sys.argv[1:] if argv is None else list(argv)
    return netwatch.main(["poll", "--interval", "300", "--count", "160", *extra])


if __name__ == "__main__":
    sys.exit(main())
