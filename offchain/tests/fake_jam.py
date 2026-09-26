"""A fake JAM chain behind a fake JIP-2 node, for deploy.py and dex_setup.py tests.

Blocks come one per advance(); each block's posterior state is kept, so a read names the
block it reads (the lookup anchor is two blocks back of the best, the finalized block one).
A submitted work-package is accumulated `delay` blocks later (or reported Failed when
the test drops it); a submitted preimage is integrated in the next block if its request
is outstanding (GP 0.8.0 "Preimage Integration").

Two services have behaviour:

  the Bootstrap service (id 0)   decodes CreateService with a parser written here from
                                 the layout recorded off `jamt create-service` (not with
                                 deploy.py), and runs GP `new`: the requested id if it is
                                 the registrar and the id is below 2^16 and free, else the
                                 next free public id; the new account requests its code.
  a jamswap-like service         LIST / REGISTER / DEPOSIT with the service's storage keys
                                 and idempotency rules (service/src/lib.rs, deposit.rs).

Knobs: bootstrap_is_registrar, bootstrap_holds_code (it provides the code itself, as when
jamt solicited it first), bootstrap_refuses, drop (packages to report Failed), lose
(packages that vanish: their status read is an error), delay.
"""
import base64, copy, hashlib, struct

from fake_jip2 import FakeJip2Node, RpcError

B64 = lambda b: base64.b64encode(b).decode()    # noqa: E731
UNB64 = base64.b64decode


def H(b):
    return hashlib.blake2b(bytes(b), digest_size=32).digest()


AUTH_CODE = b"\x05\x00the null authorizer"
BOOT_CODE = (b"\x50\x00\x15jam-bootstrap-service\x060.1.29\x0aApache-2.0\x01\x25"
             b"Parity Technologies <admin@parity.io>") + b"bootstrap pvm code"
MIN_PUBLIC = 1 << 16
PARAMS = {"core_count": 2, "max_refine_gas": 1_000_000_000, "max_accumulate_gas": 10_000_000,
          "max_lookup_anchor_age": 24, "recent_block_count": 8, "max_work_items": 16,
          "transfer_memo_size": 128, "deposit_per_item": 10, "deposit_per_byte": 1,
          "deposit_per_account": 100}


def nat(buf, i):
    """GP general natural at buf[i:], as (value, next): written for the tests."""
    b = buf[i]
    n = 0
    while n < 8 and b & (0x80 >> n):
        n += 1
    if n == 8:
        return int.from_bytes(buf[i + 1:i + 9], "little"), i + 9
    rest = int.from_bytes(buf[i + 1:i + 1 + n], "little")
    return rest + ((b & ((1 << (7 - n)) - 1)) << (8 * n)), i + 1 + n


def parse_bootstrap(payload, memo_size=128):
    """[{create-service fields}] from a Bootstrap payload: count, instructions, salt."""
    count, i = nat(payload, 0)
    out = []
    for _ in range(count):
        variant = payload[i]
        i += 1
        if variant != 0:
            raise ValueError(f"variant {variant} not modelled")
        code_hash = payload[i:i + 32]
        code_len, min_item, min_memo, endowment = struct.unpack_from("<4Q", payload, i + 32)
        i += 64
        memo = payload[i:i + memo_size]
        i += memo_size
        registration = None
        if payload[i] == 1:
            n, j = nat(payload, i + 1)
            registration, i = payload[j:j + n], j + n
        else:
            i += 1
        deposit_offset = struct.unpack_from("<Q", payload, i)[0]
        i += 8
        sid = None
        if payload[i] == 1:
            sid = struct.unpack_from("<I", payload, i + 1)[0]
            i += 5
        else:
            i += 1
        out.append(dict(code_hash=code_hash, code_len=code_len, min_item_gas=min_item,
                        min_memo_gas=min_memo, endowment=endowment, memo=memo,
                        registration=registration, deposit_offset=deposit_offset, id=sid))
    if len(payload) - i != 32:
        raise ValueError(f"expected a 32-byte salt, {len(payload) - i} bytes left")
    return out


class Account:
    def __init__(self, code_hash, created, parent, balance=10 ** 12, min_item_gas=10_000):
        self.code_hash, self.created, self.parent = code_hash, created, parent
        self.balance, self.min_item_gas = balance, min_item_gas
        self.storage, self.preimages, self.requests = {}, {}, {}

    def record(self):
        items = 2 * len(self.requests) + len(self.storage)
        octets = sum(81 + n for _, n in self.requests) + sum(34 + len(k) + len(v)
                                                            for k, v in self.storage.items())
        return struct.pack("<B32s5Q4I", 0, self.code_hash, self.balance, self.min_item_gas,
                           10_000, octets, 0, items, self.created, 0, self.parent)


class FakeJamNode(FakeJip2Node):
    GENESIS = 1000

    def __init__(self, dex_code_hashes=()):
        self.dex_code_hashes = set(dex_code_hashes)   # code hashes that behave like jamswap
        self.bootstrap_is_registrar = True
        self.bootstrap_holds_code = False
        self.bootstrap_refuses = False
        self.drop = 0                        # the next `drop` packages are reported Failed
        self.lose = 0                        # the next `lose` packages vanish
        self.delay = 2                       # blocks from submission to accumulation
        self.packages = []                   # (hash, WorkPackage-ish dict) in arrival order
        self.preimages_sent = []             # (service, blob)
        self._pending, self._status, self._preimg = [], {}, []
        boot = Account(H(BOOT_CODE), 0, 0)
        boot.preimages = {H(AUTH_CODE): AUTH_CODE, H(BOOT_CODE): BOOT_CODE}
        self.accounts = {0: boot}
        self.blocks = {}                     # header hash -> (slot, parent hash, accounts)
        self.chain = []                      # header hashes, oldest first
        self._make_block()
        super().__init__({
            "parameters": lambda: {"V1": dict(PARAMS)},
            "bestBlock": lambda: self._desc(self.chain[-1]),
            "finalizedBlock": lambda: self._desc(self.chain[max(0, len(self.chain) - 2)]),
            "parent": self._parent,
            "stateRoot": lambda hh: B64(H(b"root" + self._known(hh))),
            "beefyRoot": lambda hh: B64(H(b"beefy" + self._known(hh))),
            "listServices": lambda hh: sorted(self._at(hh)),
            "serviceData": lambda hh, s: self._acct(hh, s, lambda a: B64(a.record())),
            "serviceValue": lambda hh, s, k: self._acct(
                hh, s, lambda a: B64(a.storage[UNB64(k)]) if UNB64(k) in a.storage else None),
            "servicePreimage": lambda hh, s, p: self._acct(
                hh, s, lambda a: B64(a.preimages[UNB64(p)]) if UNB64(p) in a.preimages else None),
            "serviceRequest": lambda hh, s, p, n: self._acct(
                hh, s, lambda a: a.requests.get((UNB64(p), n))),
            "submitWorkPackage": self._submit,
            "submitPreimage": self._submit_preimage,
            "workPackageStatus": self._package_status,
        })

    # -- blocks
    def _make_block(self):
        slot = self.GENESIS + len(self.chain)
        parent = self.chain[-1] if self.chain else bytes(32)
        hh = H(b"block" + slot.to_bytes(4, "little"))
        self.blocks[hh] = (slot, parent, copy.deepcopy(self.accounts))
        self.chain.append(hh)
        return hh

    @property
    def slot(self):
        return self.blocks[self.chain[-1]][0]

    def _desc(self, hh):
        return {"header_hash": B64(hh), "slot": self.blocks[hh][0]}

    def _known(self, hh):
        h = UNB64(hh)
        if h not in self.blocks:
            raise RpcError(1, "Block unavailable", hh)
        return h

    def _parent(self, hh):
        p = self.blocks[self._known(hh)][1]
        if p not in self.blocks:
            raise RpcError(1, "Block unavailable", hh)
        return self._desc(p)

    def _at(self, hh):
        return self.blocks[self._known(hh)][2]

    def _acct(self, hh, s, fn):
        a = self._at(hh).get(s)
        return None if a is None else fn(a)

    def advance(self, n=1):
        for _ in range(n):
            slot = self.slot + 1
            for s, blob in self._preimg:             # E_P: requested, not yet provided
                a = self.accounts.get(s)
                if a is not None and a.requests.get((H(blob), len(blob))) == []:
                    a.preimages[H(blob)] = blob
                    a.requests[(H(blob), len(blob))] = [slot]
            self._preimg = []
            due = [p for p in self._pending if p[0] <= slot]
            self._pending = [p for p in self._pending if p[0] > slot]
            for _, ph, pkg in due:
                self._accumulate(pkg, slot)
                self._status[ph] = {"Ready": {"reported_in": {"header_hash": B64(bytes(32)), "slot": slot - 1},
                                              "core": 0, "report_hash": B64(bytes(32)),
                                              "ready_in": {"header_hash": B64(bytes(32)), "slot": slot}}}
            self._make_block()

    # -- submission
    def _submit(self, core, package, extrinsics):
        import workpackage
        raw = UNB64(package)
        pkg = workpackage.WorkPackage.decode(raw)
        ph = H(raw)
        self.packages.append((ph, pkg))
        if self.drop:
            self.drop -= 1
            self._status[ph] = {"Failed": "dropped by the test"}
        elif self.lose:
            self.lose -= 1
            self._status[ph] = "lost"
        else:
            self._pending.append((self.slot + self.delay, ph, pkg))
        return None

    def _package_status(self, hh, ph, anchor):
        s = self._status.get(UNB64(ph), {"Reportable": {"remaining_blocks": 8}})
        if s == "lost":
            raise RpcError(0, "anchor too old")
        return s

    def _submit_preimage(self, requester, blob):
        blob = UNB64(blob)
        self.preimages_sent.append((requester, blob))
        self._preimg.append((requester, blob))
        return None

    # -- accumulation
    def _accumulate(self, pkg, slot):
        for item in pkg.items:
            a = self.accounts.get(item.service)
            if a is None or a.code_hash != item.code_hash:
                continue
            if item.service == 0:
                self._bootstrap(item.payload, slot)
            elif a.code_hash in self.dex_code_hashes:
                self._dex(a, item.payload)

    def _bootstrap(self, payload, slot):
        if self.bootstrap_refuses:
            return
        for c in parse_bootstrap(payload):
            want = c["id"]
            if self.bootstrap_is_registrar and want is not None and want < MIN_PUBLIC:
                if want in self.accounts:
                    continue                         # FULL
                sid = want
            else:
                sid = MIN_PUBLIC + 7
                while sid in self.accounts:
                    sid += 1
            a = Account(c["code_hash"], slot, 0, balance=c["endowment"],
                        min_item_gas=c["min_item_gas"])
            a.requests[(c["code_hash"], c["code_len"])] = []
            if self.bootstrap_holds_code:            # it provides from its own store
                blob = self.accounts[0].preimages.get(c["code_hash"])
                if blob is not None:
                    a.preimages[c["code_hash"]] = blob
                    a.requests[(c["code_hash"], c["code_len"])] = [slot]
            self.accounts[sid] = a

    @staticmethod
    def _dex(a, p):
        st = a.storage
        u32 = lambda x: struct.pack("<I", x)                            # noqa: E731
        if p[0] == 6 and len(p) == 13:                                  # LIST
            m, base, quote = struct.unpack_from("<III", p, 1)
            if b"mkt" + u32(m) not in st:
                st[b"mkt" + u32(m)] = u32(base) + u32(quote)
                st[b"markets"] = st.get(b"markets", b"") + u32(m)
        elif p[0] == 7 and len(p) == 97:                                # REGISTER
            from nacl.signing import VerifyKey
            pk = p[1:33]
            VerifyKey(pk).verify(b"jamswap:v1:register" + pk, p[33:97])
            if b"h" + pk not in st:
                nxt = struct.unpack("<I", st.get(b"nexthandle", u32(1)))[0]
                st[b"pk" + u32(nxt)], st[b"h" + pk] = pk, u32(nxt)
                st[b"nexthandle"] = u32(nxt + 1)
        elif p[0] == 1 and len(p) == 25:                                # DEPOSIT
            acct, asset, amount, nonce = struct.unpack_from("<IIQQ", p, 1)
            v = st.get(b"dn" + u32(acct), bytes(8))
            floor = struct.unpack_from("<Q", v)[0]
            window = sorted(struct.unpack_from("<Q", v, i)[0] for i in range(8, len(v), 8))
            if nonce <= floor or nonce in window:
                return
            window = sorted(window + [nonce])
            if len(window) > 16:
                floor = window.pop(0)
            st[b"dn" + u32(acct)] = struct.pack(f"<{1 + len(window)}Q", floor, *window)
            k = b"b" + u32(asset) + u32(acct)
            st[k] = struct.pack("<Q", struct.unpack("<Q", st.get(k, bytes(8)))[0] + amount)

    # -- test helpers
    def packages_for(self, service):
        return [(ph, pkg) for ph, pkg in self.packages if pkg.items[0].service == service]

    def storage(self, sid):
        return self.accounts[sid].storage


class FakeClock:
    """time for Deployer / Runner: each sleep advances the chain one block (6 s)."""
    def __init__(self, node):
        self.node, self.t = node, 0.0

    def sleep(self, secs):
        self.t += 6.0
        self.node.advance()

    def clock(self):
        return self.t
