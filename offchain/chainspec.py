"""What the DEX needs from a JIP-4 chain spec: the authorizer a work-package can use.

A JIP-4 chain spec (github.com/polkadot-fellows/JIPs, "Chainspec file") is the JSON file
every JAM client starts a network from; its `genesis_state` maps 31-byte state keys (hex)
to serialized values, per GP 0.8.0 appendix D (merklization.tex). JIP-2 has no call that
reads the authorizer pools, so the genesis state is where a client-neutral builder learns
which authorizer the cores accept:

  C(1)            -> E([var(pool) for each core])        the authorizer pools alpha
  C(255, s)       -> the account record of service s
  C(s, E4(2^32-2) ++ h) -> the preimage of h held by service s

An authorizer is H(code_hash ++ config). Its code must be a preimage held by the service
named as the package's auth code host. For every service s and every preimage p of s in
genesis, if H(H(p)) (the empty configuration) is in some core's pool, then
(s, H(p), config=b"") authorizes packages on those cores. An authorizer whose
configuration is not empty cannot be recognised this way (its config is not in state);
name it explicitly instead (AUTHORIZER=host:code_hash:config[:token]).

The pools change after genesis only when a privileged service's accumulate assigns new
queue entries, so genesis tells the truth for a chain whose authorizer queue is never
reassigned (a dev net's Bootstrap arrangement). Anything else should be named explicitly.
"""
import json

from workpackage import Authorizer, blake2b256, dec_nat

PREIMAGE_TAG = ((1 << 32) - 2).to_bytes(4, "little")


def state_key(s, h):
    """C(s, h): the service id's four octets interleaved with H(h)'s first four, then 23
    more octets of H(h)."""
    n, a = s.to_bytes(4, "little"), blake2b256(h)
    return bytes([n[0], a[0], n[1], a[1], n[2], a[2], n[3], a[3]]) + a[4:27]


def load(path):
    with open(path) as f:
        return json.load(f)


def genesis_state(spec):
    """The genesis state as {31-byte key: value bytes}."""
    def unhex(x):
        return bytes.fromhex(x[2:] if x.startswith("0x") else x)
    out = {unhex(k): unhex(v) for k, v in spec["genesis_state"].items()}
    bad = [k.hex() for k in out if len(k) != 31]
    if bad:
        raise ValueError(f"chain spec: state keys must be 31 bytes: {bad[:3]}")
    return out


def auth_pools(state):
    """alpha, per core: a list of pool entries (32-byte authorizer hashes)."""
    raw = state.get(bytes([1]) + bytes(30))
    if raw is None:
        raise ValueError("chain spec: no authorizer pools (state key C(1)) in genesis")
    pools, i = [], 0
    while i < len(raw):
        n, i = dec_nat(raw, i)
        if i + 32 * n > len(raw):
            raise ValueError("chain spec: truncated authorizer pool")
        pools.append([raw[i + 32 * k:i + 32 * k + 32] for k in range(n)])
        i += 32 * n
    return pools


def service_ids(state):
    """The services with an account record C(255, s) = [255, n0, 0, n1, 0, n2, 0, n3, 0, ...]."""
    return sorted(int.from_bytes(k[1:8:2], "little") for k in state
                  if k[0] == 255 and not any(k[2:8:2]) and not any(k[8:]))


def authorizers(spec):
    """[(Authorizer, [cores])]: each genesis preimage whose empty-configuration authorizer
    is in some core's pool, with the cores whose pool holds it (ascending)."""
    state = genesis_state(spec)
    pools = auth_pools(state)
    found = []
    for s in service_ids(state):
        for key, blob in state.items():
            code_hash = blake2b256(blob)
            if key != state_key(s, PREIMAGE_TAG + code_hash):
                continue
            auth = Authorizer(s, code_hash)
            cores = [c for c, pool in enumerate(pools) if auth.hash in pool]
            if cores:
                found.append((auth, cores))
    return sorted(found, key=lambda ac: (ac[0].host, ac[0].code_hash))


def authorizer(spec):
    """The one authorizer the genesis pools accept, as (Authorizer, cores). With several,
    the one accepted on the most cores (then the lowest host id) wins."""
    found = authorizers(spec)
    if not found:
        raise ValueError("chain spec: no genesis preimage is an empty-configuration "
                         "authorizer in any core's pool; name one with AUTHORIZER")
    return max(found, key=lambda ac: (len(ac[1]), -ac[0].host))
