"""GP 0.8.0 work-packages: the types and their codec, Python stdlib only.

A work-package (GP 0.8.0 section 14.3, work_packages_and_reports.tex eq. workpackage) is

    p = (j authorization token, h auth code host, u auth code hash, f authorizer config,
         c refinement context, w work-items)

and it is serialized (appendix C, serialization.tex) as

    E(p)  = E4(h) ++ u ++ E(c) ++ var(j) ++ var(f) ++ var(w)
    E(c)  = a ++ E4(t) ++ s ++ b ++ l ++ E4(lt) ++ ls ++ var(prerequisites)
            (anchor hash, anchor slot, anchor posterior state root, anchor accumulation-
             output super-peak (JIP-2 "beefyRoot"), lookup-anchor hash, lookup-anchor slot,
             lookup-anchor posterior state root, prerequisite package hashes)
    E(w)  = E4(s) ++ c ++ E8(g) ++ E8(a) ++ E2(e) ++ var(y) ++ var(I#(imports))
            ++ var([h ++ E4(n) for each extrinsic])
            (service, code hash, refine gas, accumulate gas, export count, payload, ...)

where var(x) is x prefixed with its length as a general natural E(len(x)), En is n-octet
little-endian, and a package's hash is Blake2b-256 of its encoding (h = H(p) in the
work-report computation). The two gas sums must stay under the chain's limits:
sum(refine) < G_R and sum(accumulate) < G_A (eq. wplimits).

The work-package's authorizer is H(u ++ f) (GP 0.8.0 section 14.3); a guarantee is valid
only if it is in the authorizer pool of the core it is reported on (eq.
reportcoresareunused), and the authorizer code is the preimage of u in service h's
preimages at the lookup anchor.
"""
import hashlib
from typing import NamedTuple, Tuple

ZERO_HASH = bytes(32)


def blake2b256(data):
    return hashlib.blake2b(bytes(data), digest_size=32).digest()


# ---- the codec (GP 0.8.0 appendix C) -----------------------------------------
def enc_nat(x):
    """The general natural serialization E(x), 1 to 9 octets, for 0 <= x < 2^64."""
    if not 0 <= x < 1 << 64:
        raise ValueError(f"natural out of range: {x}")
    if x == 0:
        return b"\x00"
    for l in range(8):
        if x < 1 << (7 * (l + 1)):
            prefix = 256 - (1 << (8 - l)) + (x >> (8 * l))
            return bytes([prefix]) + (x & ((1 << (8 * l)) - 1)).to_bytes(l, "little")
    return b"\xff" + x.to_bytes(8, "little")


def dec_nat(buf, i=0):
    """Decode E(x) at buf[i:]; return (x, next index)."""
    if i >= len(buf):
        raise ValueError("truncated natural")
    b = buf[i]
    l = 8 if b == 0xFF else next(n for n in range(8) if not b & (0x80 >> n))
    if i + 1 + l > len(buf):
        raise ValueError("truncated natural")
    rest = int.from_bytes(buf[i + 1:i + 1 + l], "little")
    x = rest if l == 8 else rest + ((b & ((1 << (7 - l)) - 1)) << (8 * l))
    if enc_nat(x) != bytes(buf[i:i + 1 + l]):
        raise ValueError(f"non-canonical natural {bytes(buf[i:i + 1 + l]).hex()}")
    return x, i + 1 + l


def var(blob):
    return enc_nat(len(blob)) + bytes(blob)


def _u(x, n):
    return int(x).to_bytes(n, "little")


def _hash(h, what):
    h = bytes(h)
    if len(h) != 32:
        raise ValueError(f"{what}: expected 32 bytes, got {len(h)}")
    return h


class _Reader:
    def __init__(self, buf):
        self.buf, self.i = bytes(buf), 0

    def take(self, n):
        if self.i + n > len(self.buf):
            raise ValueError(f"truncated at offset {self.i} (wanted {n} more bytes)")
        out = self.buf[self.i:self.i + n]
        self.i += n
        return out

    def u(self, n):
        return int.from_bytes(self.take(n), "little")

    def nat(self):
        x, self.i = dec_nat(self.buf, self.i)
        return x

    def blob(self):
        return self.take(self.nat())

    def seq(self, item):
        return tuple(item(self) for _ in range(self.nat()))

    def end(self):
        if self.i != len(self.buf):
            raise ValueError(f"{len(self.buf) - self.i} trailing bytes")


# ---- the types ----------------------------------------------------------------
class RefineContext(NamedTuple):
    """GP 0.8.0 eq. workcontext. The anchor must be one of the last H blocks, with its
    posterior state root and accumulation-output super-peak exactly as recent history
    records them; the lookup anchor must be an ancestor at most L slots old whose child's
    prior state root is `lookup_anchor_state_root`."""
    anchor: bytes
    anchor_slot: int
    state_root: bytes
    beefy_root: bytes              # the accumulation-output log super-peak
    lookup_anchor: bytes
    lookup_anchor_slot: int
    lookup_anchor_state_root: bytes
    prerequisites: Tuple[bytes, ...] = ()

    def encode(self):
        prereq = sorted(_hash(p, "prerequisite") for p in self.prerequisites)   # a set: ordered
        return (_hash(self.anchor, "anchor") + _u(self.anchor_slot, 4)
                + _hash(self.state_root, "state_root") + _hash(self.beefy_root, "beefy_root")
                + _hash(self.lookup_anchor, "lookup_anchor") + _u(self.lookup_anchor_slot, 4)
                + _hash(self.lookup_anchor_state_root, "lookup_anchor_state_root")
                + enc_nat(len(prereq)) + b"".join(prereq))

    @classmethod
    def read(cls, r):
        return cls(r.take(32), r.u(4), r.take(32), r.take(32), r.take(32), r.u(4), r.take(32),
                   r.seq(lambda r: r.take(32)))


class ImportRef(NamedTuple):
    """An imported segment: `root` is a segment root, or (by_package) the hash of the
    exporting work-package; I(r, i) encodes the latter with bit 15 of the index set."""
    root: bytes
    index: int
    by_package: bool = False

    def encode(self):
        if not 0 <= self.index < 1 << 15:
            raise ValueError(f"import index out of range: {self.index}")
        return _hash(self.root, "import root") + _u(self.index | (self.by_package << 15), 2)

    @classmethod
    def read(cls, r):
        root, i = r.take(32), r.u(2)
        return cls(root, i & 0x7FFF, bool(i >> 15))


class WorkItem(NamedTuple):
    """GP 0.8.0 eq. workitem; `extrinsics` holds (hash, length) pairs."""
    service: int
    code_hash: bytes
    payload: bytes
    refine_gas: int
    accumulate_gas: int
    export_count: int = 0
    imports: Tuple[ImportRef, ...] = ()
    extrinsics: Tuple[Tuple[bytes, int], ...] = ()

    def encode(self):
        return (_u(self.service, 4) + _hash(self.code_hash, "code_hash")
                + _u(self.refine_gas, 8) + _u(self.accumulate_gas, 8) + _u(self.export_count, 2)
                + var(self.payload)
                + enc_nat(len(self.imports)) + b"".join(ImportRef(*i).encode() for i in self.imports)
                + enc_nat(len(self.extrinsics))
                + b"".join(_hash(h, "extrinsic hash") + _u(n, 4) for h, n in self.extrinsics))

    @classmethod
    def read(cls, r):
        # the wire order puts the gas limits and export count before the payload
        service, code_hash = r.u(4), r.take(32)
        refine_gas, accumulate_gas, export_count = r.u(8), r.u(8), r.u(2)
        payload = r.blob()
        return cls(service, code_hash, payload, refine_gas, accumulate_gas, export_count,
                   r.seq(ImportRef.read), r.seq(lambda r: (r.take(32), r.u(4))))


class Authorizer(NamedTuple):
    """Who authorizes a package: the service `host` whose preimages hold the authorizer
    code, that code's hash, the configuration blob, and the token the package carries.
    Its identity in a core's authorizer pool is H(code_hash ++ config)."""
    host: int
    code_hash: bytes
    config: bytes = b""
    token: bytes = b""

    @property
    def hash(self):
        return blake2b256(_hash(self.code_hash, "authorizer code hash") + bytes(self.config))

    @classmethod
    def parse(cls, text):
        """`host:code_hash_hex[:config_hex[:token_hex]]`, e.g. from AUTHORIZER."""
        parts = [p.strip() for p in text.strip().split(":")]
        if not 2 <= len(parts) <= 4:
            raise ValueError(f"authorizer {text!r}: expected host:code_hash[:config[:token]]")
        blobs = [bytes.fromhex(p[2:] if p.startswith("0x") else p) for p in parts[1:]]
        auth = cls(int(parts[0]), *blobs)
        _hash(auth.code_hash, "authorizer code hash")
        return auth

    def __str__(self):
        return ":".join([str(self.host), self.code_hash.hex()]
                        + ([self.config.hex(), self.token.hex()] if self.config or self.token else []))


class WorkPackage(NamedTuple):
    """GP 0.8.0 eq. workpackage."""
    auth_code_host: int
    auth_code_hash: bytes
    context: RefineContext
    items: Tuple[WorkItem, ...]
    authorization: bytes = b""             # the token j
    authorizer_config: bytes = b""         # f

    @classmethod
    def build(cls, authorizer, context, items):
        return cls(authorizer.host, authorizer.code_hash, context, tuple(items),
                   authorizer.token, authorizer.config)

    def encode(self):
        if not self.items:
            raise ValueError("a work-package carries at least one work-item")
        return (_u(self.auth_code_host, 4) + _hash(self.auth_code_hash, "auth_code_hash")
                + self.context.encode() + var(self.authorization) + var(self.authorizer_config)
                + enc_nat(len(self.items)) + b"".join(w.encode() for w in self.items))

    @property
    def authorizer(self):
        return blake2b256(self.auth_code_hash + bytes(self.authorizer_config))

    def hash(self):
        return blake2b256(self.encode())

    @classmethod
    def decode(cls, data):
        r = _Reader(data)
        host, code_hash = r.u(4), r.take(32)
        ctx = RefineContext.read(r)
        token, config = r.blob(), r.blob()
        items = r.seq(WorkItem.read)
        r.end()
        return cls(host, code_hash, ctx, items, token, config)
