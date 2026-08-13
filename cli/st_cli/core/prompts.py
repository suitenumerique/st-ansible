"""Shared questionary-based interactive input primitives.

Extracted from :mod:`st_cli.cmd.bootstrap` so that core code (e.g.
:mod:`st_cli.core.secretbackend`) can prompt without importing up into
``cmd`` (which would reverse the strict ``main → cmd → core`` layering).

Output goes through :mod:`st_cli.core.ui` (rich); input goes through here.
"""

from __future__ import annotations

import ipaddress
import re
from contextlib import contextmanager
from dataclasses import dataclass

import questionary
from prompt_toolkit.formatted_text import FormattedText

from . import ui
from .errors import StCliError

# Dim grey for ghost-hint placeholder text (questionary's default `class:placeholder`
# renders near-white). `italic` reinforces "this is a hint, not a value".
_PLACEHOLDER_STYLE = "fg:#6c6c6c italic"


class Recovered(str):
    """Marker for a prompt default recovered from the committed tree.

    Behaves exactly like ``str``. Silent-replay mode auto-accepts a non-empty
    ``Recovered`` default without a prompt; a plain ``str`` default always asks.
    """


@dataclass
class ReplayStats:
    """Counters for a :func:`silent_replay` run."""

    auto: int = 0
    asked: int = 0


# The single active ReplayStats, or None outside silent mode. Mirrors the
# module's existing style (see core/sshuser.py's `_checked`): one flat module
# variable, no stack — nested silent_replay() calls are not supported.
_active_stats: ReplayStats | None = None
_header_shown = False


def in_silent_replay() -> bool:
    """True while a :func:`silent_replay` context is active."""
    return _active_stats is not None


@contextmanager
def silent_replay():
    """Activate silent-replay mode: auto-accept recovered defaults, ask the rest.

    Yields the :class:`ReplayStats` for this run. Deactivates on exit, including
    on an exception, so a failure never leaves the module stuck in silent mode.
    """
    global _active_stats, _header_shown
    stats = ReplayStats()
    _active_stats = stats
    _header_shown = False
    try:
        yield stats
    finally:
        _active_stats = None
        _header_shown = False


@contextmanager
def suspend_silent():
    """Temporarily turn off silent mode inside an active :func:`silent_replay`.

    Use around a fresh provider's sub-questionnaire, which must ask every
    question regardless of the outer replay. A no-op when silent mode is not
    active.
    """
    global _active_stats
    if _active_stats is None:
        yield
        return
    saved = _active_stats
    _active_stats = None
    try:
        yield
    finally:
        _active_stats = saved


def _announce_silent_question() -> None:
    """Print the one-time "new settings" header before the first real prompt."""
    global _header_shown
    if not _header_shown:
        ui.info("This release asks about new settings:")
        _header_shown = True


def _require(text) -> bool | str:
    """questionary validator: reject empty / whitespace-only input."""
    return True if (text or "").strip() else "A value is required."


def _text_question(
    prompt: str,
    default: str = "",
    required: bool = True,
    placeholder: str | None = None,
):
    """Build a questionary.text Question (does NOT prompt).

    Factored out of :func:`_ask` so the prompt wiring is unit-testable without a
    TTY. A non-empty ``default`` renders as questionary's NATIVE editable
    pre-filled value (same colour as typed text; Enter accepts it or the operator
    edits inline) — any ``placeholder`` passed alongside is ignored so lingering
    call sites degrade gracefully. A ``placeholder`` with NO ``default`` renders
    as a grey italic ghost hint in an empty field (an example, not a value) that
    must be typed over. With neither, the field starts plain and empty.
    """
    if default:
        # native editable pre-fill — no custom styling; validate honours `required`.
        return questionary.text(
            prompt, default=default, validate=_require if required else None
        )
    if placeholder is not None:
        # Explicit dim grey on the fragment itself — questionary's default style
        # leaves `class:placeholder` near-white, so we set the colour inline so it
        # reads as a hint regardless of the active style sheet.
        return questionary.text(
            prompt,
            placeholder=FormattedText([(_PLACEHOLDER_STYLE, placeholder)]),
            validate=_require if required else None,
        )
    return questionary.text(prompt, default="", validate=_require if required else None)


def _ask(
    prompt: str,
    default: str = "",
    required: bool = True,
    placeholder: str | None = None,
) -> str:
    """Ask a free-text question. Non-empty by default; pass required=False for
    genuinely optional fields. A non-empty ``default`` renders as questionary's
    native editable pre-filled value (Enter accepts it, or edit inline). A
    ``placeholder`` with no ``default`` shows a grey italic ghost hint in an empty
    field that must be typed over (use for example-style prompts).

    In silent-replay mode, a non-empty :class:`Recovered` default is kept without
    a prompt. Any other default (empty, or a plain fallback like ``"5432"``)
    still prompts — a brand-new question always asks. The one exception is
    ``required=False``: blank is always a safe answer for an optional field, so
    silent mode auto-accepts ``default`` (recovered, plain, or empty) with no
    prompt — this also covers a recovered optional whose committed template
    omits the line when blank, so recovery cannot hand back a ``Recovered``
    value for it.
    """
    if _active_stats is not None:
        if (isinstance(default, Recovered) and default.strip()) or not required:
            _active_stats.auto += 1
            return str(default).strip()
        _announce_silent_question()
        _active_stats.asked += 1
    ans = _text_question(
        prompt, default=default, required=required, placeholder=placeholder
    ).ask()
    if ans is None:
        raise StCliError("bootstrap cancelled by user.")
    return ans.strip()


def _password(prompt: str, required: bool = True) -> str:
    """Ask a hidden-input question (non-empty by default).

    Never auto-accepts in silent-replay mode: a recovered secret never reaches
    this primitive (the questionnaire's secret-recovery path returns early), so
    any call here is a genuinely new question.
    """
    if _active_stats is not None:
        _announce_silent_question()
        _active_stats.asked += 1
    ans = questionary.password(prompt, validate=_require if required else None).ask()
    if ans is None:
        raise StCliError("bootstrap cancelled by user.")
    return ans


def _confirm(prompt: str, default: bool = False, auto: bool = True) -> bool:
    """Yes/no confirmation; raises on cancel.

    In silent-replay mode with ``auto`` True (the default), returns ``default``
    without a prompt. This is safe because every gating confirm derives its
    default from recovered state (CLAUDE.md, §6): auto-accepting it replays the
    operator's existing choice, never a fresh decision. Pass ``auto=False`` for
    a genuinely new boolean question (a destructive confirm, for example), which
    must still ask even in silent mode.
    """
    if _active_stats is not None:
        if auto:
            _active_stats.auto += 1
            return default
        _announce_silent_question()
        _active_stats.asked += 1
    ans = questionary.confirm(prompt, default=default).ask()
    if ans is None:
        raise StCliError("bootstrap cancelled by user.")
    return ans


_HOSTNAME_RE = re.compile(
    r"^(?=.{1,253}$)(?!-)[A-Za-z0-9-]{1,63}(?<!-)(\.(?!-)[A-Za-z0-9-]{1,63}(?<!-))*$"
)


def _is_valid_host(h: str) -> bool:
    """True if ``h`` is a valid IP address or hostname.

    IP validation uses the stdlib :mod:`ipaddress`. Crucially, anything that
    *looks like* an IPv4/IPv6 attempt — IPv6 (``:``), a purely numeric/dotted
    string, or a dotted-decimal start like ``10.1.1.`` — must parse as a real IP,
    so typos like ``10.1.1.a`` or ``10.2.2.2.2.2`` are rejected instead of being
    accepted as (technically legal) hostnames. Everything else is checked as a
    hostname.
    """
    looks_like_ip = (
        ":" in h
        or re.fullmatch(r"[0-9.]+", h) is not None
        or re.match(r"^\d+\.\d+\.\d+\.", h) is not None
    )
    if looks_like_ip:
        try:
            ipaddress.ip_address(h)
            return True
        except ValueError:
            return False
    return bool(_HOSTNAME_RE.match(h))


def _ask_hosts(
    label: str, allow_empty: bool = False, default: list[str] | None = None
) -> list[str]:
    """Prompt for comma-separated hosts (IP or hostname), validated inline.

    When ``allow_empty`` is True, a blank answer is accepted and returns ``[]``
    (used for optional worker hosts that default to the core's hosts). Any
    entered hosts are still validated with :func:`_is_valid_host`.

    ``default`` (rebootstrap) pre-fills the field with the operator's current
    hosts as questionary's native editable pre-fill — same rationale as
    ``_text_question``'s ``default``: Enter keeps it, or the operator edits
    inline. It is joined with ``", "`` (a comma + space) for readability; the
    parser (``[h.strip() for h in raw.split(",") if h.strip()]``) strips
    whitespace either way, so a bare ``","`` join would round-trip identically
    but reads worse. A falsy ``default`` (``None`` or ``[]``) omits the
    pre-fill entirely, matching today's behaviour exactly.

    In silent-replay mode, a non-empty ``default`` is kept without a prompt; an
    empty ``default`` with ``allow_empty`` returns ``[]`` (what Enter would give
    interactively); an empty ``default`` that is required still prompts.
    """
    if _active_stats is not None:
        if default:
            _active_stats.auto += 1
            return list(default)
        if allow_empty:
            _active_stats.auto += 1
            return []
        _announce_silent_question()
        _active_stats.asked += 1

    def validate(raw: str):
        hosts = [h.strip() for h in raw.split(",") if h.strip()]
        if not hosts:
            if allow_empty:
                return True
            return "Enter at least one host (IP address or hostname)."
        bad = [h for h in hosts if not _is_valid_host(h)]
        if bad:
            return "Not a valid IP/hostname: " + ", ".join(bad)
        return True

    raw = questionary.text(
        f"{label} host(s) — IP or hostname, comma-separated",
        default=", ".join(default) if default else "",
        validate=validate,
    ).ask()
    if raw is None:
        raise StCliError("bootstrap cancelled by user.")
    return [h.strip() for h in raw.split(",") if h.strip()]


def _ask_select(
    message: str, choices: list[str], default: str | None = None, auto: bool = True
) -> str:
    """Single-choice questionary select; raises if the user bails out.

    ``default`` (rebootstrap) pre-selects the operator's current answer — but
    ``questionary.select`` requires its ``default`` to be one of ``choices``,
    raising otherwise. A recovered config value can legitimately no longer be
    an offered choice (e.g. an OIDC provider or dependency mode removed/renamed
    since the config was written), so it is passed through only when truthy
    AND present in ``choices``; otherwise it is silently omitted, degrading to
    "no pre-selection" rather than crashing the questionnaire.

    In silent-replay mode with ``auto`` True, a ``default`` that is truthy and
    present in ``choices`` is returned without a prompt. Otherwise (no default,
    a stale default, or ``auto=False``) it still prompts — a genuine gap.
    """
    if _active_stats is not None:
        if auto and default and default in choices:
            _active_stats.auto += 1
            return default
        _announce_silent_question()
        _active_stats.asked += 1
    kwargs = {}
    if default and default in choices:
        kwargs["default"] = default
    choice = questionary.select(message, choices=choices, **kwargs).ask()
    if choice is None:
        raise StCliError("bootstrap cancelled by user.")
    return choice


def _confirm_ready(message: str = "Do you have all of the above ready?") -> None:
    """Readiness gate: a yes/no confirmation (default yes). Enter or ``y`` continues;
    ``n``, Esc, or Ctrl+C aborts the whole CLI via :class:`StCliError` (→ exit 1)
    rather than dropping the operator into the questionnaire half-prepared."""
    if not _confirm(message, default=True):
        raise StCliError(
            "bootstrap cancelled — prepare the requirements above, then re-run."
        )
