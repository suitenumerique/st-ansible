"""Ed25519 caller-keypair generation for the file-scanner JWT auth.

st-cli deliberately carries no crypto dependency, and the only primitive the
``generate-keypairs`` command needs is deriving an Ed25519 public key from a
32-byte seed — so the RFC 8032 reference math is inlined below. Keygen only:
no signing, no verification, no untrusted input, and side-channel resistance
is irrelevant for a local one-shot mint. Correctness is pinned to the RFC 8032
test vectors in ``tests/test_keypairs.py``.

Output format matches upstream file-scanner exactly (its ``jwt_auth`` parser
and ``deploy/scripts/new-issuer.py``): the raw 32-byte values as unpadded
URL-safe base64 — the private key IS the seed.
"""

from __future__ import annotations

import base64
import hashlib
import secrets

_P = 2**255 - 19


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


_D = -121665 * _inv(121666) % _P
_I = pow(2, (_P - 1) // 4, _P)


def _xrecover(y: int) -> int:
    xx = (y * y - 1) * _inv(_D * y * y + 1)
    x = pow(xx, (_P + 3) // 8, _P)
    if (x * x - xx) % _P != 0:
        x = x * _I % _P
    if x % 2 != 0:
        x = _P - x
    return x


_BY = 4 * _inv(5) % _P
_B = (_xrecover(_BY), _BY)


def _edwards_add(p: tuple[int, int], q: tuple[int, int]) -> tuple[int, int]:
    x1, y1 = p
    x2, y2 = q
    x3 = (x1 * y2 + x2 * y1) * _inv(1 + _D * x1 * x2 * y1 * y2)
    y3 = (y1 * y2 + x1 * x2) * _inv(1 - _D * x1 * x2 * y1 * y2)
    return x3 % _P, y3 % _P


def _scalarmult_base(e: int) -> tuple[int, int]:
    p, q = _B, (0, 1)  # (0, 1) is the neutral element
    while e:
        if e & 1:
            q = _edwards_add(q, p)
        p = _edwards_add(p, p)
        e >>= 1
    return q


def derive_public_key(seed: bytes) -> bytes:
    """RFC 8032 §5.1.5: 32-byte Ed25519 seed → 32-byte compressed public key."""
    h = hashlib.sha512(seed).digest()
    a = int.from_bytes(h[:32], "little")
    a &= (1 << 254) - 8  # clamp: clear the low 3 bits...
    a |= 1 << 254  # ...and set bit 254
    x, y = _scalarmult_base(a)
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def generate_keypair() -> tuple[str, str]:
    """Mint a fresh keypair → ``(private_b64url, public_b64url)``."""
    seed = secrets.token_bytes(32)
    return _b64url(seed), _b64url(derive_public_key(seed))
