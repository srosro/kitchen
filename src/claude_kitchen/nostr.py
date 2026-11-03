"""Nostr event signing: BIP-340 Schnorr over secp256k1, after the BIP's
reference code. Pure Python because the secp256k1 bindings publish no wheel
for the Python kitchen runs on; a chat's few signatures a minute don't need C.
"""
import hashlib
import json
import os
import time

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and a[1] != b[1]:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k: int):
    point, addend = None, G
    while k:
        if k & 1:
            point = _add(point, addend)
        addend = _add(addend, addend)
        k >>= 1
    return point


def _tagged(tag: str, data: bytes) -> bytes:
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + data).digest()


def _xonly(point) -> bytes:
    return point[0].to_bytes(32, "big")


def public_key(secret: bytes) -> bytes:
    return _xonly(_mul(int.from_bytes(secret, "big")))


def sign(secret: bytes, msg: bytes, aux: bytes) -> bytes:
    d0 = int.from_bytes(secret, "big")
    assert 0 < d0 < N, "secret key out of range"
    pub = _mul(d0)
    d = d0 if pub[1] % 2 == 0 else N - d0
    t = (d ^ int.from_bytes(_tagged("BIP0340/aux", aux), "big")).to_bytes(32, "big")
    k0 = int.from_bytes(_tagged("BIP0340/nonce", t + _xonly(pub) + msg), "big") % N
    r = _mul(k0)
    k = k0 if r[1] % 2 == 0 else N - k0
    e = int.from_bytes(_tagged("BIP0340/challenge", _xonly(r) + _xonly(pub) + msg), "big") % N
    return _xonly(r) + ((k + e * d) % N).to_bytes(32, "big")


def event(secret: bytes, kind: int, content: str, tags: list) -> dict:
    """A signed NIP-01 event."""
    ev = {"pubkey": public_key(secret).hex(), "created_at": int(time.time()), "kind": kind,
          "tags": tags, "content": content}
    serial = json.dumps([0, ev["pubkey"], ev["created_at"], kind, tags, content],
                        separators=(",", ":"), ensure_ascii=False)
    ev["id"] = hashlib.sha256(serial.encode()).hexdigest()
    ev["sig"] = sign(secret, bytes.fromhex(ev["id"]), os.urandom(32)).hex()
    return ev
