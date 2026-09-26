"""The standard JAM dev accounts (JIP-5), with no client binary.

Every validator on a jamswap test net is a standard dev account: validator i holds
the account whose seed is u32-LE(i) repeated 8 times. PolkaJam, JavaJAM and pbnjam
(`--dev-validator i`) and lasair (`--dev-account i`, `--own i`) all derive the same
keys from it, so one table of public keys serves every client in a layout.

What is computed here and what is looked up:
  - seed, the Ed25519 public key and the JAMNP-S peer id are DERIVED (JIP-5:
    ed25519_secret_seed = blake2b-256("jam_val_key_ed25519" ++ seed), then RFC 8032;
    peer id = "e" ++ base32 of the key read as a little-endian integer, JAMNP-S).
  - the Bandersnatch public key is LOOKED UP in the published table
    (docs.jamcha.in/basics/dev-accounts, fetched 2026-09-26): deriving it needs a
    Bandersnatch implementation, which the standard library doesn't have. The table
    covers indices 0..5, i.e. every tiny net (V = 6).

`python3 nets/devkeys.py [i ...]` prints the accounts, in `lasair --dev-account` form.
"""
import hashlib
import sys

# docs.jamcha.in/basics/dev-accounts ("The values are specified by JIP-5").
# ed25519_public and dns_alt_name are kept to check the derivation against.
PUBLISHED = [
    # (name, ed25519_public, bandersnatch_public, dns_alt_name)
    ("Alice", "4418fb8c85bb3985394a8c2756d3643457ce614546202a2f50b093d762499ace",
     "ff71c6c03ff88adb5ed52c9681de1629a54e702fc14729f6b50d2f0a76f185b3",
     "eecgwpgwq3noky4ijm4jmvjtmuzv44qvigciusxakq5epnrfj2utb"),
    ("Bob", "ad93247bd01307550ec7acd757ce6fb805fcf73db364063265b30a949e90d933",
     "dee6d555b82024f1ccf8a1e37e60fa60fd40b1958c4bb3006af78647950e1b91",
     "en5ejs5b2tybkfh4ym5vpfh7nynby73xhtfzmazumtvcijpcsz6ma"),
    ("Carol", "cab2b9ff25c2410fbe9b8a717abb298c716a03983c98ceb4def2087500b8e341",
     "9326edb21e5541717fde24ec085000b28709847b8aab1ac51f84e94b37ca1b66",
     "ekwmt37xecoq6a7otkm4ux5gfmm4uwbat4bg5m223shckhaaxdpqa"),
    ("David", "f30aa5444688b3cab47697b37d5cac5707bb3289e986b19b17db437206931a8d",
     "0746846d17469fb2f95ef365efcab9f4e22fa1feb53111c995376be8019981cc",
     "etxckkczii4mvm22ox4m3horvx2bwlzerjxbd3n6c36qehdms2idb"),
    ("Eve", "8b8c5d436f92ecf605421e873a99ec528761eb52a88a2f9a057b3b3003e6f32a",
     "151e5c8fe2b9d8a606966a79edd2f9e5db47e83947ce368ccba53bf6ba20a40b",
     "eled3vb5nse3n7cii6ybvtms5s2bdwvlkivc7cnwa33oatby4txka"),
    ("Fergie", "ab0084d01534b31c1dd87c81645fd762482a90027754041ca1b56133d0466c06",
     "2105650944fcd101621fd5bb3124c9fd191d114b7ad936c1d79d734f9f21392e",
     "elfaiiixcuzmzroa34lajwp52cdsucikaxdviaoeuvnygdi3imtba"),
]


def dev_seed(i):
    """The dev account seed: the index as a u32 little-endian, repeated 8 times."""
    return i.to_bytes(4, "little") * 8


def _blake2b256(data):
    return hashlib.blake2b(data, digest_size=32).digest()


def ed25519_secret_seed(seed):
    return _blake2b256(b"jam_val_key_ed25519" + seed)


def bandersnatch_secret_seed(seed):
    return _blake2b256(b"jam_val_key_bandersnatch" + seed)


# ---- Ed25519 public key from a secret seed (RFC 8032 section 5.1.5) ----------------
_P = 2 ** 255 - 19
_D = -121665 * pow(121666, _P - 2, _P) % _P
_GY = 4 * pow(5, _P - 2, _P) % _P
_GX_SQ = (_GY * _GY - 1) * pow(_D * _GY * _GY + 1, _P - 2, _P) % _P
_GX = pow(_GX_SQ, (_P + 3) // 8, _P)
if (_GX * _GX - _GX_SQ) % _P:
    _GX = _GX * pow(2, (_P - 1) // 4, _P) % _P
if _GX % 2:
    _GX = _P - _GX
_BASE = (_GX, _GY, 1, _GX * _GY % _P)          # extended coordinates (X, Y, Z, T)


def _add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = (y1 - x1) * (y2 - x2) % _P
    b = (y1 + x1) * (y2 + x2) % _P
    c = 2 * t1 * t2 * _D % _P
    d = 2 * z1 * z2 % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


def _mul(s, p):
    q = (0, 1, 1, 0)
    while s:
        if s & 1:
            q = _add(q, p)
        p = _add(p, p)
        s >>= 1
    return q


def ed25519_public(secret_seed):
    h = hashlib.sha512(secret_seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8
    a |= 1 << 254
    x, y, z, _ = _mul(a, _BASE)
    zi = pow(z, _P - 2, _P)
    x, y = x * zi % _P, y * zi % _P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def peer_id(ed25519_pub):
    """JAMNP-S: "e" ++ B(E_32^-1(k), 52) with the alphabet a-z2-7, low digit first."""
    alphabet = "abcdefghijklmnopqrstuvwxyz234567"
    n, out = int.from_bytes(ed25519_pub, "little"), []
    for _ in range(52):
        out.append(alphabet[n % 32])
        n //= 32
    return "e" + "".join(out)


def dev_account(i):
    """Public keys of dev account i: {name, seed, ed25519, bandersnatch, peer_id} (hex)."""
    if not 0 <= i < len(PUBLISHED):
        raise ValueError(
            "dev account %d: only 0..%d have a published Bandersnatch key (tiny nets); "
            "a larger layout needs a Bandersnatch key derivation" % (i, len(PUBLISHED) - 1))
    seed = dev_seed(i)
    ed = ed25519_public(ed25519_secret_seed(seed))
    name, _, bandersnatch, _ = PUBLISHED[i]
    return {"name": name, "seed": seed.hex(), "ed25519": ed.hex(),
            "bandersnatch": bandersnatch, "peer_id": peer_id(ed)}


if __name__ == "__main__":
    for arg in sys.argv[1:] or [str(i) for i in range(len(PUBLISHED))]:
        a = dev_account(int(arg))
        print("seed:        %s\nbandersnatch: %s\ned25519:      %s\npeer_id:      %s\n"
              % (a["seed"], a["bandersnatch"], a["ed25519"], a["peer_id"]))
