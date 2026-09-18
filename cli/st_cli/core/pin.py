"""Compare the installed CLI version against the `.st-cli.yml` pin.

`.st-cli.yml` records the CLI version last used to bootstrap the repo
(``versions.cli``). :func:`compare` tells a caller whether the installed
``st_cli`` build matches that pin, so callers can warn when the two versions
are different.
"""

from __future__ import annotations

import re
from enum import Enum

import st_cli

from . import upgrades
from .models import StCliManifest

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


class PinState(str, Enum):
    """How the installed CLI version relates to the `.st-cli.yml` pin."""

    ALIGNED = "aligned"
    CLI_OLDER = "cli_older"
    CLI_NEWER = "cli_newer"
    UNKNOWN = "unknown"


def compare(m: StCliManifest) -> PinState:
    """Compare the installed CLI version against `m.cli_version`.

    Return UNKNOWN when the pin is empty or when either version does not
    match the `X.Y.Z` shape.
    """
    if not m.cli_version:
        return PinState.UNKNOWN
    if not _VERSION_RE.match(m.cli_version) or not _VERSION_RE.match(
        st_cli.__version__
    ):
        return PinState.UNKNOWN
    installed = upgrades.parse_version(st_cli.__version__)
    pinned = upgrades.parse_version(m.cli_version)
    if installed == pinned:
        return PinState.ALIGNED
    if installed < pinned:
        return PinState.CLI_OLDER
    return PinState.CLI_NEWER
