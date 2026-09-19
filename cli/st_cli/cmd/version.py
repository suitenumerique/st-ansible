"""`st-cli version`: print the installed and pinned versions."""

from __future__ import annotations

from .. import __version__ as CLI_VERSION
from ..core import manifest, ui
from ..core.errors import StCliError


def show_version() -> None:
    """Print the installed cli version and the .st-cli.yml pins.

    The pin and upstream warnings come from the global callback
    (``core.upstream.maybe_warn_upgrade``), like for every other subcommand.
    """
    ui.info(f"st-cli (installed): {CLI_VERSION}")
    try:
        m = manifest.load_manifest()
    except StCliError:
        ui.info("No .st-cli.yml in this directory (not a deployment repo).")
        return
    ui.info(
        f".st-cli.yml pins  : collection={m.collection_version} cli={m.cli_version}"
    )
