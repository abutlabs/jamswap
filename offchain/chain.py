"""The DEX backend's one door to the chain: a small client-neutral interface, two backends.

Everything the off-chain layer needs from a JAM chain goes through a `Chain`:

    head()             the best block, as a Block(slot, hash, height)
    finalized()        the latest finalized block, as a Block
    read(key, at)      a value in the service's storage (b"" when absent), at "best",
                       "final", or an explicit header hash
    submit(payload)    get one work-item payload for the service onto the chain
    submit_items(ps)   get up to `max_items` payloads onto the chain together, accumulated
                       in order (one work-package on jip2; jamnp takes one at a time)
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
                   serviceData, parameters. Submission builds a GP 0.8.0 work-package
                   (workpackage.py) and sends it with submitWorkPackage; the authorizer
                   comes from AUTHORIZER, or from the JIP-4 chain spec at CHAIN_SPEC
                   (chainspec.py).

SERVICE_ID names the service. It comes from genesis (lasair nets) or from a runtime
deploy through the chain's Bootstrap service over JIP-2 (deploy.py, issue #13).
"""
import json, os, time, urllib.request
from typing import NamedTuple, Optional

import chainspec
import jip2
import workpackage


class ChainError(Exception):
    """The chain could not serve the request."""


class ChainBusy(ChainError):
    """Every guarantor refused the submission (their work-package queues are full), or the
    backend could not read what it needs to build it: the payload never reached the chain.
    Callers either retry later (the round builder re-queues the round's orders) or surface
    it as HTTP 503 (user ops)."""


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
    max_items = 1              # payloads submit_items() takes at once

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

    def submit_items(self, payloads):
        """Relay up to `max_items` payloads so that they land together and are accumulated
        in the order given; same outcome rules as submit(). A backend that sends one
        payload at a time takes a single one."""
        payloads = list(payloads)
        if len(payloads) != 1:
            raise ChainUnsupported(f"{self.name}: {len(payloads)} payloads at once "
                                   f"(max_items is {self.max_items})")
        return self.submit(payloads[0])

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
        # the reader can serve a read: it has learned a head, and (a reader that proves its
        # reads, lasair#70) a block it can prove state at, `best_hex`: the head's parent,
        # none while the node is still at genesis, where every read is refused
        try:
            r = _http_json(self._need(self.reader_url, "READER_URL") + "/healthz")
        except (OSError, ValueError):
            return False
        return bool(r.get("head_hex")) and bool(r.get("best_hex", True))

    def read(self, key, at="best"):
        if at != "best":
            raise ChainUnsupported("jamnp: the reader bridge reads only at the head it "
                                   "follows (no finalized or historical reads)")
        r = _http_json(self._need(self.reader_url, "READER_URL")
                       + f"/read?service={self._sid()}&key={key.hex()}")
        if r.get("error"):
            # the reader could not read at all (node unreachable, no head yet): that is not
            # an absent key — reading it as b"" would show an empty book, zero balances and
            # no seq floors. Raise, as an unreachable bridge does, so callers fail closed.
            raise ChainError(f"jamnp reader: {r['error']}")
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

    def __init__(self, service_id=None, url="ws://localhost:19800", timeout=30.0, client=None,
                 authorizer=None, chain_spec=None, cores=None, refine_gas=None,
                 accumulate_gas=None):
        super().__init__(service_id)
        self.url = url
        self.rpc = client or jip2.Jip2Client(url, timeout)
        self._init_submission(authorizer, chain_spec, cores, refine_gas, accumulate_gas)

    def describe(self):
        return f"jip2 ({self.url}; service {self.service_id})"

    def for_service(self, service_id):
        """The same node, connection and submission settings, for another service (the
        chain's Bootstrap service, say)."""
        return Jip2Chain(service_id, self.url, client=self.rpc, authorizer=self.authorizer,
                         chain_spec=self.chain_spec, cores=self.cores,
                         refine_gas=self.refine_gas, accumulate_gas=self.accumulate_gas)

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
        info = self.service_record(self._sid(), at)
        if info is None:
            raise ChainError(f"jip2: no service {self._sid()} at that block")
        return info

    def parameters(self):
        return self._call(self.rpc.parameters)

    # ---- any service's account, preimages and requests (runtime deploy: deploy.py) ----
    def services(self, at="best"):
        """The service ids the node lists (JIP-2 listServices: best effort)."""
        return [int(s) for s in self._call(self.rpc.list_services, self._at(at))]

    def service_record(self, service_id, at="best"):
        """The account record of `service_id`, or None if there is no such service."""
        data = self._call(self.rpc.service_data, self._at(at), service_id)
        return None if data is None else ServiceInfo.decode(data)

    def preimage(self, service_id, preimage_hash, at="best"):
        """The preimage of `preimage_hash` provided to `service_id`, or None."""
        return self._call(self.rpc.service_preimage, self._at(at), service_id, preimage_hash)

    def preimage_request(self, service_id, preimage_hash, length, at="best"):
        """None (neither requested nor provided), [] (requested, not provided) or the slots
        of its history (JIP-2 serviceRequest)."""
        return self._call(self.rpc.service_request, self._at(at), service_id, preimage_hash, length)

    def provide(self, service_id, preimage):
        """Hand a preimage that `service_id` has requested to the node, for a block's
        preimage extrinsic (JIP-2 submitPreimage; it does not wait for inclusion)."""
        return self._call(self.rpc.submit_preimage, service_id, preimage)

    def parameter(self, name, default=None):
        """One chain parameter (JIP-2 parameters().V1), fetched once; `default`, if given,
        when the node does not report it."""
        if default is not None and name not in self._load_params():
            return default
        return self._param(name)

    # ---- submission: one GP 0.8.0 work-package per submit, via submitWorkPackage ----
    #
    # The package carries one work-item per payload, all for the service: its code hash as
    # the chain holds it at the anchor, the payload, no imports, extrinsics or exports, and
    # an even share of the largest gas limits a package may have (sum(refine) < G_R,
    # sum(accumulate) < G_A: GP 0.8.0 eq. wplimits); a lone payload gets all of it, so no
    # payload runs out of refine gas (a signed order costs ~5.3M to verify). Items are
    # refined one by one and accumulated in order, in one accumulate call for the service
    # whose budget is the sum of theirs (GP 0.8.0 section 12, accumulation.tex). Its
    # refinement context names the best block's parent as the anchor and the finalized
    # block's parent as the lookup anchor (see default_anchor and default_lookup_anchor);
    # no prerequisites. The core is the next one, round robin, whose authorizer pool
    # accepts the authorizer: one report per core per block, so consecutive packages
    # spread over the cores.

    def _init_submission(self, authorizer, chain_spec, cores, refine_gas, accumulate_gas):
        # authorizer: an Authorizer, or its "host:code_hash[:config[:token]]" text; else
        # chain_spec: a JIP-4 chain spec (path or parsed dict) whose genesis names it.
        # cores: the cores to use (default: those whose genesis pool accepts it, else all).
        # refine_gas / accumulate_gas: the work-item's limits (default: the most allowed).
        if isinstance(authorizer, str):
            authorizer = workpackage.Authorizer.parse(authorizer)
        self.authorizer, self.chain_spec = authorizer, chain_spec
        self.cores = list(cores) if cores else None
        self.refine_gas, self.accumulate_gas = refine_gas, accumulate_gas
        self._params = None
        self._auth_checked = False
        self._next_core = 0

    @property
    def submits(self):
        return self.authorizer is not None or self.chain_spec is not None

    @property
    def max_items(self):
        """I, the most work-items a package may carry (JIP-2 parameters; 1 if the node
        does not report it)."""
        return self.parameter("max_work_items", 1)

    def _load_params(self):
        # the chain parameters (JIP-2 parameters().V1), fetched once
        if self._params is None:
            p = self.parameters()
            if not isinstance(p, dict) or not isinstance(p.get("V1"), dict):
                raise ChainError(f"jip2: parameters: no V1 member in {p!r}")
            self._params = p["V1"]
        return self._params

    def _param(self, name):
        self._load_params()
        try:
            return int(self._params[name])
        except (KeyError, TypeError, ValueError):
            raise ChainError(f"jip2: parameters: no number {name}") from None

    def _resolve_authorizer(self):
        if self.authorizer is None:
            if self.chain_spec is None:
                raise ChainUnsupported(
                    "jip2: no authorizer to submit with: set AUTHORIZER "
                    "(host:code_hash[:config[:token]]) or CHAIN_SPEC (a JIP-4 chain spec)")
            try:
                spec = self.chain_spec
                self.authorizer, genesis_cores = chainspec.authorizer(
                    chainspec.load(spec) if isinstance(spec, str) else spec)
            except (OSError, ValueError, KeyError, TypeError) as e:
                raise ChainUnsupported(f"jip2: no authorizer from the chain spec: {e}") from e
            self.cores = self.cores or genesis_cores
        if self.cores is None:
            self.cores = list(range(self._param("core_count")))
        return self.authorizer

    def _check_authorizer(self, at):
        # The authorizer's code must be a preimage of its host service, or every guarantor
        # drops the package without a word (JIP-2 has no refusal for it). Checked once; a
        # node without servicePreimage leaves it unchecked.
        if self._auth_checked:
            return
        a = self.authorizer
        try:
            code = self.rpc.service_preimage(at, a.host, a.code_hash)
        except jip2.Jip2Error as e:
            if e.code != -32601:               # anything but "method not found": try again later
                raise ChainError(f"jip2 servicePreimage: {e}") from e
            code = b""                         # the node cannot tell: leave it unchecked
        except (OSError, ValueError) as e:
            raise ChainError(f"jip2 servicePreimage: {e}") from e
        if code is None:
            raise ChainUnsupported(
                f"jip2: authorizer {a}: service {a.host} holds no preimage of its code hash "
                "on this chain (AUTHORIZER or CHAIN_SPEC is for another chain?)")
        self._auth_checked = True

    def _parent_of(self, block):
        # the block's parent, or the block itself if it has none (the genesis block)
        try:
            d = self.rpc.parent(block.hash)
        except jip2.Jip2Error:
            return block
        except (OSError, ValueError) as e:
            raise ChainError(f"jip2 parent: {e}") from e
        try:
            return Block(int(d["slot"]), jip2.unb64(d["header_hash"]), None)
        except (KeyError, TypeError, ValueError) as e:
            raise ChainError(f"jip2: malformed parent descriptor {d!r}") from e

    def default_anchor(self):
        """The block a new package anchors at: the best block's parent. The anchor must
        be in recent history with its state root, and recent history holds the newest
        block with a zero state root until its child corrects it (GP 0.8.0 eq.
        correctlaststateroot), so the parent is the newest block whose recorded state
        root is final."""
        return self._parent_of(self.head())

    def default_lookup_anchor(self, anchor):
        """The lookup anchor for a package anchored at `anchor`: the finalized block's
        parent. A lookup anchor must be in the finalized chain (GP 0.8.0 overview, "The Core
        Model and Services"), have a child whose prior state root is its posterior one (the
        ancestor rule under eq. limitlookupanchorage), and be at most L slots older than
        the block that reports it; the finalized block's parent is the newest block that
        is all three whether or not the best block is itself finalized. When finality
        lags so far that it would be too old by the time the report lands (a margin of H
        slots), the anchor itself is used instead: young enough, but not finalized, so a
        guarantor that holds to the finality rule refuses it (a net without finality
        cannot do better)."""
        lookup = self._parent_of(self.finalized())
        margin = self._param("max_lookup_anchor_age") - self._param("recent_block_count")
        return anchor if lookup.slot + margin < anchor.slot else lookup

    def refine_context(self, anchor, lookup_anchor):
        """The refinement context of a package anchored at `anchor` with `lookup_anchor`
        (Blocks): each one's posterior state root, the anchor's BEEFY root (its
        accumulation-output super-peak); no prerequisites."""
        state_root = self._call(self.rpc.state_root, anchor.hash)
        return workpackage.RefineContext(
            anchor.hash, anchor.slot, state_root, self._call(self.rpc.beefy_root, anchor.hash),
            lookup_anchor.hash, lookup_anchor.slot,
            state_root if lookup_anchor.hash == anchor.hash
            else self._call(self.rpc.state_root, lookup_anchor.hash))

    def work_package(self, payload, anchor=None, lookup_anchor=None):
        """The encoded work-package that carries `payload` for the service, and its refine
        context. `anchor` and `lookup_anchor` (Blocks) default to default_anchor() and
        default_lookup_anchor()."""
        return self.work_package_items([payload], anchor, lookup_anchor)

    def work_package_items(self, payloads, anchor=None, lookup_anchor=None):
        """As work_package, with one work-item per payload, in order. The gas limits are
        shared evenly (explicit refine_gas / accumulate_gas are per item)."""
        sid = self._sid()
        payloads = [bytes(p) for p in payloads]
        n = len(payloads)
        if n < 1 or n > 1 and n > self.max_items:
            raise ChainUnsupported(f"jip2: {n} work-items; a package carries 1 to "
                                   f"{self.max_items}")
        auth = self._resolve_authorizer()
        anchor = anchor or self.default_anchor()
        lookup_anchor = lookup_anchor or self.default_lookup_anchor(anchor)
        self._check_authorizer(anchor.hash)
        ctx = self.refine_context(anchor, lookup_anchor)
        info = self.service_info(anchor.hash)
        g_r, g_a = self._param("max_refine_gas"), self._param("max_accumulate_gas")
        refine = self.refine_gas or (g_r - 1) // n
        accumulate = self.accumulate_gas or (g_a - 1) // n
        if n * refine >= g_r or n * accumulate >= g_a:
            raise ChainUnsupported(f"jip2: {n} items of {refine} refine / {accumulate} "
                                   f"accumulate gas exceed the package limits G_R={g_r}, G_A={g_a}")
        if accumulate < info.min_item_gas:
            raise ChainUnsupported(f"jip2: service {sid} wants at least {info.min_item_gas} "
                                   f"accumulate gas per item; the package allows {accumulate}")
        items = [workpackage.WorkItem(sid, info.code_hash, p, refine, accumulate) for p in payloads]
        return workpackage.WorkPackage.build(auth, ctx, items).encode(), ctx

    def submit(self, payload):
        """Send `payload` in its own work-package to the guarantors of the next core that
        accepts the authorizer; on a refusal (JIP-2: the package reached no guarantor) try
        the next core. The receipt names the package, core and anchor, for
        package_status(). ChainBusy: nothing was sent, because every core refused or the
        node could not serve the reads that build the package. Settlement is seen in the
        service's state, as for any backend."""
        return self.submit_items([payload])

    def submit_items(self, payloads):
        """As submit, with one work-package that carries every payload as a work-item, in
        order (at most max_items)."""
        self._sid()                            # configuration errors stay ChainError
        payloads = list(payloads)
        t0 = time.monotonic()
        try:
            package, ctx = self.work_package_items(payloads)
        except (ChainBusy, ChainUnsupported):
            raise
        except ChainError as e:
            raise ChainBusy(f"not sent: {e}") from e
        package_hash = workpackage.blake2b256(package)
        t1 = time.monotonic()             # the context's reads done: the anchor is chosen
        refused = []
        for k in range(len(self.cores)):
            core = self.cores[(self._next_core + k) % len(self.cores)]
            try:
                self.rpc.submit_work_package(core, package)
            except jip2.Jip2Error as e:
                if e.code == -32601:           # JSON-RPC "method not found"
                    raise ChainUnsupported(f"jip2: the node has no submitWorkPackage: {e}") from e
                refused.append(f"core {core}: {e}")
                continue
            except (OSError, ValueError) as e:
                raise ChainError(f"jip2 submitWorkPackage: outcome unknown: {e}") from e
            self._next_core = (self._next_core + k + 1) % len(self.cores)
            return {"accepted": True, "package_hash": package_hash.hex(), "core": core,
                    # seconds spent building the package (its reads) and sending it: the
                    # anchor's window runs from the build, so both count against it
                    "build_seconds": round(t1 - t0, 3),
                    "send_seconds": round(time.monotonic() - t1, 3),
                    "anchor": ctx.anchor.hex(), "anchor_slot": ctx.anchor_slot,
                    "lookup_anchor": ctx.lookup_anchor.hex(),
                    "lookup_anchor_slot": ctx.lookup_anchor_slot, "refused": refused}
        raise ChainBusy("every core refused the package: " + "; ".join(refused))

    def package_status(self, receipt, at="best"):
        """JIP-2 workPackageStatus of a submitted package at `at`: {"Reportable": ...},
        {"Reported": ...}, {"Ready": ...} or {"Failed": reason}. Ready is not
        accumulated: watch the service's state for that."""
        return self._call(self.rpc.work_package_status, self._at(at),
                          bytes.fromhex(receipt["package_hash"]), bytes.fromhex(receipt["anchor"]))


# ---- selection ---------------------------------------------------------------
BACKENDS = ("jamnp", "jip2")


def from_env(env=None):
    """The backend the environment selects. CHAIN_BACKEND defaults to jamnp, configured
    by BUILDER_URL / READER_URL / NODE_METRICS_URL; jip2 is configured by CHAIN_RPC, and
    submits with the authorizer AUTHORIZER names or CHAIN_SPEC's genesis holds.
    Nothing touches the network here."""
    env = os.environ if env is None else env
    sid = int(env["SERVICE_ID"]) if env.get("SERVICE_ID") else None
    backend = (env.get("CHAIN_BACKEND") or "jamnp").strip().lower()
    if backend == "jamnp":
        return JamnpChain(sid, env.get("BUILDER_URL", ""), env.get("READER_URL", ""),
                          env.get("NODE_METRICS_URL", ""))
    if backend == "jip2":
        return Jip2Chain(sid, env.get("CHAIN_RPC") or "ws://localhost:19800",
                         authorizer=env.get("AUTHORIZER") or None,
                         chain_spec=env.get("CHAIN_SPEC") or None)
    raise ValueError(f"CHAIN_BACKEND={backend!r}: expected one of {', '.join(BACKENDS)}")
