# Monitor exporter image: stdlib-only Python. Build context is the repo root.
#
#   netwatch (offchain/netwatch.py, issue #15): the client-neutral poller — every node's
#     JIP-2 RPC (or lasair's metrics endpoint), one-head / finality agreement and the
#     on-chain validator statistics as jam_* metrics on :9106. Runs when NETWATCH_NODES
#     is set (NAME,CLIENT,URL[,READER] specs; see netwatch.py).
#   exporter.py (legacy, mixed net only): PolkaJam log scraping through the Docker socket
#     plus pj0's RPC on :9105. Runs when NETWATCH_NODES is unset, so the existing mixed
#     monitor overlay is unchanged until it sets NETWATCH_NODES.
FROM python:3.12-alpine
COPY monitor/exporter.py /exporter.py
COPY offchain/netwatch.py offchain/chain.py offchain/jip2.py offchain/chainspec.py \
     offchain/workpackage.py /netwatch/
EXPOSE 9105 9106
CMD ["sh", "-c", "if [ -n \"$NETWATCH_NODES\" ]; then exec python3 -u /netwatch/netwatch.py serve; else exec python3 -u /exporter.py; fi"]
