"""The DEX backend's one door to the chain: a small client-neutral interface, two backends.

Everything the off-chain layer needs from a JAM chain goes through a `Chain`:

    head()             the best block, as a Block(slot, hash, height)
    finalized()        the latest finalized block, as a Block
    read(key, at)      a value in the service's storage (b"" when absent), at "best",
                       "final", or an explicit header hash
    submit(payload)    get one work-item payload for the service onto the chain
    service_info(at)   the service's account record (code hash, balance, footprint ...)
    parameters()       the chain parameters
    ready()            can the backend serve reads yet?

A Block field is None where the backend cannot observe it, and a method the backend
cannot serve raises ChainUnsupported (a NotImplementedError), so a caller can degrade
the way it already does for a missing capability.

Backends (CHAIN_BACKEND):

  jamnp (default)  JAMNP-S through two local HTTP bridges, exactly as the DEX has used
                   them: BUILDER_URL (POST /submit -> a work-package over CE-133) and
                   READER_URL (GET /read -> a CE-129 state request at the head the bridge
                   follows). Heads and finality come from the node's Prometheus gauges at
                   NODE_METRICS_URL, when set. The bridges and the gauge names are lasair's
                   (lasair has no JIP-2 server yet); this backend retires once it does.
  jip2             JIP-2 node RPC (JSON-RPC over WebSocket) at CHAIN_RPC, default
                   ws://localhost:19800: bestBlock, finalizedBlock, serviceValue,
                   serviceData, parameters. Submission needs a spec-valid work-package
                   (anchor, authorizer, core) and is not built yet (issue #11).

SERVICE_ID names the service. Runtime deployment is not part of the interface yet
(issue #13): the id comes from genesis or from an out-of-band deploy.
"""
import json, os, time, urllib.request
from typing import NamedTuple, Optional

import jip2


class ChainError(Exception):
    """The chain could not serve the request."""


class ChainBusy(ChainError):
    """Every guarantor refused the submission (their work-package queues are full): the
    payload never reached the chain. Callers either retry later (the round builder
    re-queues the round's orders) or surface it as HTTP 503 (user ops)."""


class ChainUnsupported(ChainError, NotImplementedError):
    """This backend cannot serve this request (yet)."""


class Block(NamedTuple):
    slot: Optional[int]
    hash: Optional[bytes]
    height: Optional[int]      # blocks since genesis; JIP-2 block descriptors carry none


class ServiceInfo(NamedTuple):
    """A service account record, C(255, s) in GP 0.8.0 (merklization.tex): 89 bytes,
    E(0, code_hash, E8(balance, min_item_gas, min_memo_gas, octets, gratis),
      E4(items, created, last_accumulated, parent))."""
    code_hash: bytes
    balance: int
    min_item_gas: int          # minimum accumulate gas per work-item
    min_memo_gas: int          # minimum on-transfer gas per memo
    octets: int                # storage footprint in octets
    gratis: int                # deposit offset
    items: int                 # storage items
    created: int               # slot of creation
    last_accumulated: int      # slot of the most recent accumulation
    parent: int                # the creating service

    SIZE = 89

    @classmethod
    def decode(cls, data):
        if len(data) != cls.SIZE or data[0] != 0:
            raise ChainError(f"service record: expected {cls.SIZE} bytes with version 0, "
                             f"got {len(data)} bytes starting {data[:1].hex() or '(empty)'}")
        e8 = [int.from_bytes(data[33 + 8 * i:41 + 8 * i], "little") for i in range(5)]
        e4 = [int.from_bytes(data[73 + 4 * i:77 + 4 * i], "little") for i in range(4)]
        return cls(bytes(data[1:33]), *e8, *e4)


class Chain:
    """The interface. Backends override what they can serve."""
    name = "chain"
    submits = False            # can submit() get a payload onto the chain?

    def __init__(self, service_id=None):
        self.service_id = service_id

    def describe(self):
        return f"{self.name} (service {self.service_id})"

    def ready(self):
        raise ChainUnsupported(f"{self.name}: ready")

    def head(self):
        raise ChainUnsupported(f"{self.name}: head")

    def finalized(self):
        raise ChainUnsupported(f"{self.name}: finalized")

    def read(self, key, at="best"):
        raise ChainUnsupported(f"{self.name}: read")

    def submit(self, payload):
        """Relay one work-item payload for the service; return the backend's receipt (a
        dict). ChainBusy and ChainUnsupported mean the payload certainly did not reach the
        chain; any other exception leaves the outcome unknown (it may still land), so a
        caller watches state rather than resubmitting blindly. Submission is at-least-once:
        the service must stay idempotent under a duplicate."""
        raise ChainUnsupported(f"{self.name}: submit")

    def service_info(self, at="best"):
        raise ChainUnsupported(f"{self.name}: service_info")

    def parameters(self):
        raise ChainUnsupported(f"{self.name}: parameters")

    def _sid(self):
        if self.service_id is None:
            raise ChainError("no service id: set SERVICE_ID")
        return self.service_id


# ---- jamnp: lasair's CE-133 builder + CE-129 reader bridges -----------------
def _http_json(url, body=None, timeout=30):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"content-type": "application/json"} if data else {})
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())


class JamnpChain(Chain):
    name = "jamnp"
    submits = True
    METRICS_TTL = 1.0          # head() and finalized() back to back share one scrape

    def __init__(self, service_id=None, builder_url="", reader_url="", metrics_url=""):
        super().__init__(service_id)
        self.builder_url = builder_url.rstrip("/")
        self.reader_url = reader_url.rstrip("/")
        self.metrics_url = metrics_url.strip()
        self._scrape = (0.0, None)

    def describe(self):
        return (f"jamnp (builder {self.builder_url or '-'}, reader {self.reader_url or '-'}, "
                f"metrics {self.metrics_url or '-'}; service {self.service_id})")

    def _need(self, url, var):
        if not url:
            raise ChainError(f"jamnp backend: {var} is not set "
                             f"(or choose CHAIN_BACKEND=jip2 with CHAIN_RPC)")
        return url

    def ready(self):
        # the reader has learned a head, so CE-129 reads will be served
        try:
            r = _http_json(self._need(self.reader_url, "READER_URL") + "/healthz")
        except (OSError, ValueError):
            return False
        return bool(r.get("head_hex"))

    def read(self, key, at="best"):
        if at != "best":
            raise ChainUnsupported("jamnp: the reader bridge reads only at the head it "
                                   "follows (no finalized or historical reads)")
        r = _http_json(self._need(self.reader_url, "READER_URL")
                       + f"/read?service={self._sid()}&key={key.hex()}")
        return bytes.fromhex(r["value_hex"]) if r.get("value_hex") else b""

    def submit(self, payload):
        r = _http_json(self._need(self.builder_url, "BUILDER_URL") + "/submit",
                       {"service_id": self._sid(), "payload_hex": payload.hex()})
        if r.get("accepted") is False:
            raise ChainBusy("all guarantors refused (CE-133 queues full)")
        return r

    def _gauges(self):
        # the node's Prometheus text: {name: value} for the gauges named lasair_*
        t, g = self._scrape
        now = time.time()
        if g is not None and now - t < self.METRICS_TTL:
            return g
        txt = urllib.request.urlopen(self.metrics_url, timeout=2).read().decode()
        g = {}
        for ln in txt.splitlines():
            if ln.startswith("lasair_") and " " in ln:
                k, _, val = ln.partition(" ")
                try:
                    g[k] = float(val)
                except ValueError:
                    pass
        self._scrape = (now, g)
        return g

    def head(self):
        # None when no metrics endpoint is configured; a gauge the node does not export
        # reads as 0. The bridges expose no head hash beside the gauges.
        if not self.metrics_url:
            return None
        g = self._gauges()
        return Block(int(g.get("lasair_slot", 0)), None, int(g.get("lasair_block_height", 0)))

    def finalized(self):
        if not self.metrics_url:
            return None
        g = self._gauges()
        return Block(int(g.get("lasair_finalized_slot", 0)), None,
                     int(g.get("lasair_finalized_height", 0)))

    def service_info(self, at="best"):
        raise ChainUnsupported("jamnp: no CE-129 read of the service account record yet")

    def parameters(self):
        raise ChainUnsupported("jamnp: the bridges do not expose chain parameters")


# ---- jip2: the JIP-2 node RPC ------------------------------------------------
class Jip2Chain(Chain):
    name = "jip2"

    def __init__(self, service_id=None, url="ws://localhost:19800", timeout=30.0, client=None):
        super().__init__(service_id)
        self.url = url
        self.rpc = client or jip2.Jip2Client(url, timeout)

    def describe(self):
        return f"jip2 ({self.url}; service {self.service_id})"

    def _call(self, fn, *args):
        # the node refused (Jip2Error), the transport failed (OSError), or the answer was
        # not what JIP-2 says it is (ValueError: bad base64 / JSON)
        try:
            return fn(*args)
        except (jip2.Jip2Error, OSError, ValueError) as e:
            raise ChainError(f"jip2 {getattr(fn, '__name__', fn)}: {e}") from e

    def _block(self, fn):
        d = self._call(fn)
        try:
            return Block(int(d["slot"]), jip2.unb64(d["header_hash"]), None)
        except (KeyError, TypeError, ValueError) as e:
            raise ChainError(f"jip2: malformed block descriptor {d!r}") from e

    def ready(self):
        try:
            self.head()
            return True
        except ChainError:
            return False

    def head(self):
        return self._block(self.rpc.best_block)

    def finalized(self):
        return self._block(self.rpc.finalized_block)

    def _at(self, at):
        # the header hash a state query runs against
        if at == "best":
            return self.head().hash
        if at == "final":
            return self.finalized().hash
        if isinstance(at, Block):
            return at.hash
        if isinstance(at, (bytes, bytearray)) and len(at) == 32:
            return bytes(at)
        raise ValueError(f"at must be 'best', 'final', a Block or a 32-byte header hash, not {at!r}")

    def read(self, key, at="best"):
        v = self._call(self.rpc.service_value, self._at(at), self._sid(), key)
        return v or b""

    def service_info(self, at="best"):
        data = self._call(self.rpc.service_data, self._at(at), self._sid())
        if data is None:
            raise ChainError(f"jip2: no service {self._sid()} at that block")
        return ServiceInfo.decode(data)

    def parameters(self):
        return self._call(self.rpc.parameters)

    def submit(self, payload):
        raise ChainUnsupported(
            "jip2: submission is not implemented yet: submitWorkPackage needs a spec-valid "
            "work-package (refine context from the chain, an authorizer the chain accepts, "
            "a core); tracked in abutlabs/jamswap#11")


# ---- selection ---------------------------------------------------------------
BACKENDS = ("jamnp", "jip2")


def from_env(env=None):
    """The backend the environment selects. CHAIN_BACKEND defaults to jamnp, configured
    by BUILDER_URL / READER_URL / NODE_METRICS_URL; jip2 is configured by CHAIN_RPC.
    Nothing touches the network here."""
    env = os.environ if env is None else env
    sid = int(env["SERVICE_ID"]) if env.get("SERVICE_ID") else None
    backend = (env.get("CHAIN_BACKEND") or "jamnp").strip().lower()
    if backend == "jamnp":
        return JamnpChain(sid, env.get("BUILDER_URL", ""), env.get("READER_URL", ""),
                          env.get("NODE_METRICS_URL", ""))
    if backend == "jip2":
        return Jip2Chain(sid, env.get("CHAIN_RPC") or "ws://localhost:19800")
    raise ValueError(f"CHAIN_BACKEND={backend!r}: expected one of {', '.join(BACKENDS)}")
