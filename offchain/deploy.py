#!/usr/bin/env python3
"""One-shot: deploy the Jamswap service blob ($JAM) to the JAM node ($LASAIR_RPC)."""
import json, os, urllib.request

jam = open(os.environ["JAM"], "rb").read()
req = urllib.request.Request(
    os.environ["LASAIR_RPC"].rstrip("/") + "/v1/service",
    data=json.dumps({"jam_hex": jam.hex()}).encode(),
    headers={"content-type": "application/json"},
)
print("deployed jamswap-service:", urllib.request.urlopen(req, timeout=30).read().decode())
