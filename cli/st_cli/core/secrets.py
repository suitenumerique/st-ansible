"""Secret/credential generation helpers.

Used by bootstrap to mint Django ``SECRET_KEY``s, API keys/tokens and random
passwords. Values are URL-safe so they survive being embedded in env blobs.
"""

from __future__ import annotations

import secrets as _secrets
import string

# Django-style alphabet for SECRET_KEY (avoids shell/quote-hostile chars).
_DJANGO_ALPHABET = string.ascii_letters + string.digits + "!@#$%^&*(-_=+)"


def gen_secret(length: int = 64) -> str:
    """Return a Django-style ``SECRET_KEY`` of the given character length."""
    return "".join(_secrets.choice(_DJANGO_ALPHABET) for _ in range(length))


def gen_token(nbytes: int = 32) -> str:
    """Return a URL-safe token suitable for API keys."""
    return _secrets.token_urlsafe(nbytes)


def gen_password(nbytes: int = 24) -> str:
    """Return a URL-safe random password."""
    return _secrets.token_urlsafe(nbytes)
