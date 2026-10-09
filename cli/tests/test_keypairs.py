"""Tests for st_cli.core.keypairs — pure-python Ed25519 public-key derivation."""

from __future__ import annotations

import base64
import secrets

# A TEST dependency only (declared in the [dev] extra): the differential check
# below must never silently skip, so the import is hard — a missing lib fails
# collection loudly instead of quietly shrinking coverage.
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from st_cli.core import keypairs

# RFC 8032 §7.1 test vectors (TEST 1 / TEST 2 / TEST 3): secret seed → public key.
_RFC8032_VECTORS = [
    (
        "9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
        "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a",
    ),
    (
        "4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
        "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c",
    ),
    (
        "c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
        "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
    ),
]


def test_derive_public_key_matches_rfc8032_vectors():
    for seed_hex, pub_hex in _RFC8032_VECTORS:
        assert keypairs.derive_public_key(bytes.fromhex(seed_hex)).hex() == pub_hex


def test_generate_keypair_format_and_roundtrip():
    """Both halves are unpadded base64url of raw 32-byte values (the upstream
    new-issuer.py format), and the public half re-derives from the private seed."""
    private, public = keypairs.generate_keypair()
    for value in (private, public):
        assert len(value) == 43  # 32 bytes → 43 base64url chars, no padding
        assert "=" not in value
        raw = base64.urlsafe_b64decode(value + "=")
        assert len(raw) == 32
    seed = base64.urlsafe_b64decode(private + "=")
    assert keypairs.derive_public_key(seed) == base64.urlsafe_b64decode(public + "=")


def test_generate_keypair_is_random():
    assert keypairs.generate_keypair() != keypairs.generate_keypair()


def test_derive_public_key_matches_cryptography_lib():
    """Differential check against pyca/cryptography (a [dev]-extra test dep):
    random + edge seeds must derive byte-identical public keys."""
    seeds = [bytes(32), bytes([0xFF]) * 32]  # edge: all-zeros, all-ones
    # 25 random seeds ≈ 3 s (each pure-python derivation costs ~130 ms); a
    # systematic math error would fail on every seed anyway.
    seeds += [secrets.token_bytes(32) for _ in range(25)]
    for seed in seeds:
        ref = (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        assert keypairs.derive_public_key(seed) == ref, seed.hex()
