"""`st-cli version` — print versions and warn on manifest/installed mismatch."""

from __future__ import annotations

from ..core import manifest, ui
from ..core.errors import StCliError
from .. import __version__ as CLI_VERSION


def show_version() -> None:
    """Print installed cli/collection versions; warn if .st-cli.yml disagrees."""
    ui.info(f"st-cli (installed): {CLI_VERSION}")
    try:
        m = manifest.load_manifest()
    except StCliError:
        ui.info("No .st-cli.yml in this directory (not a deployment repo).")
        return
    ui.info(
        f".st-cli.yml pins  : collection={m.collection_version} cli={m.cli_version}"
    )
    if m.cli_version and m.cli_version != CLI_VERSION:
        ui.warn(
            f"Pinned cli {m.cli_version} != installed {CLI_VERSION}. "
            "Run `st-cli upgrade` (or align versions) before deploying."
        )
