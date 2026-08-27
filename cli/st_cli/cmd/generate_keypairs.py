"""``st-cli generate-keypairs APP ENV`` — mint Ed25519 caller keypairs.

CLI twin of file-scanner's ``deploy/scripts/new-issuer.py``: a small
questionnaire (issuer name per keypair, loop for several callers) followed by a
wiring summary. For each caller it prints the ``iss:pubkey`` fragment to put in
file-scanner's ``JWT_ISSUER_KEYS`` — merged with the value already present in
the bootstrapped unit's env blob, if any — and the private key to hand to that
caller (e.g. transfers uses it as ``SCAN_JWT_PRIVATE_KEY``).

st-cli keeps NO copy of the private keys: they are displayed once. That is the
point — the scanner side only ever stores public keys, and the private half
belongs to the caller's own unit/vault, which this command does not own.
"""

from __future__ import annotations

from ..core import appmeta, keypairs, paths, tree, ui
from ..core.errors import StCliError
from ..core.prompts import _ask, _confirm

__all__ = ["generate"]


def _existing_issuer_keys(meta, env: str) -> str | None:
    """Current JWT_ISSUER_KEYS value from the bootstrapped unit's env blob.

    Returns ``None`` when the unit is not bootstrapped yet (the normal case:
    keypairs are minted BEFORE bootstrap, which prompts for JWT_ISSUER_KEYS) or
    when the blob carries no such line.
    """
    core = meta.core()
    if not paths.vars_path(meta.app, env, core.key).exists():
        return None
    spec = meta.env_render_spec(core.key)
    blob_var = spec["backend"]["blob_var"]
    blob = tree.load_vars(meta.app, env, core.key).get(blob_var) or ""
    for line in blob.splitlines():
        if line.startswith("JWT_ISSUER_KEYS="):
            return line.split("=", 1)[1].strip() or None
    return None


def generate(app: str, env: str) -> None:
    """Run the keypair questionnaire for ``(app, env)`` and print the summary."""
    meta = appmeta.load_app(app)  # unknown app → clean StCliError with the list
    if app != "file-scanner":
        raise StCliError(
            "generate-keypairs currently supports only file-scanner — the app "
            "that authenticates its callers by Ed25519 keypair."
        )

    ui.info(f"Minting caller keypair(s) for {app}/{env}.")
    pairs: list[tuple[str, str, str]] = []  # (iss, private, public)
    while True:
        iss = _ask("Issuer name (the calling app's `iss` claim)", "transferts")
        if ":" in iss or "," in iss:
            # ':' and ',' are the JWT_ISSUER_KEYS delimiters — re-prompt.
            ui.warn("Issuer name must not contain ':' or ','.")
            continue
        if any(p[0] == iss for p in pairs):
            ui.warn(f"Issuer {iss!r} already minted in this run — pick another name.")
            continue
        private, public = keypairs.generate_keypair()
        pairs.append((iss, private, public))
        ui.success(f"{iss}: keypair generated.")
        if not _confirm("Generate another caller keypair?", default=False):
            break

    existing = _existing_issuer_keys(meta, env)
    fragments = [f"{iss}:{public}" for iss, _, public in pairs]
    merged = ",".join(([existing] if existing else []) + fragments)

    core_key = meta.core().key
    if existing is not None:
        target = (
            f"update JWT_ISSUER_KEYS in {app}/{env}/{core_key}/vars.yml "
            f"(existing keys kept), then `st-cli deploy {app} {env}`"
        )
    elif paths.vars_path(app, env, core_key).exists():
        target = (
            f"add a JWT_ISSUER_KEYS line to {app}/{env}/{core_key}/vars.yml, "
            f"then `st-cli deploy {app} {env}`"
        )
    else:
        target = f"paste when `st-cli bootstrap {app} {env}` asks for JWT_ISSUER_KEYS"

    # Guidance in a panel; the actual values OUTSIDE it via ui.value — a Panel
    # would hard-fold long keys and break copy-paste.
    ui.note(
        f"[bold]{app} side[/bold]: set JWT_ISSUER_KEYS to the value below — {target}.\n"
        "JWT_ISSUER_KEYS is comma-separated: to add a caller to an already-set "
        "value, append `,iss:pubkey` to it.\n"
        "[bold]caller side[/bold]: hand each private key to its app over a secure "
        "channel (transfers uses SCAN_JWT_PRIVATE_KEY).\n"
        "This command is purely [bold]local[/bold]: it only generates and prints — "
        "nothing is written to the repo or the vault, no network is involved, and "
        "st-cli keeps [bold]no copy[/bold] of the private keys (shown only once, "
        "below).",
        title="Keypairs",
    )
    ui.info("JWT_ISSUER_KEYS:")
    ui.value(merged)
    for iss, private, _ in pairs:
        ui.info(f"{iss} private key:")
        ui.value(private)
