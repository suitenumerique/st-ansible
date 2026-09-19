"""Shared questionary-based interactive input primitives.

Core code prompts through here instead of importing up into `cmd`. Output
goes through `core/ui.py`; input goes through here.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Callable
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

    Behaves like `str`. Silent-replay mode auto-accepts a non-empty
    `Recovered` default without asking; a plain `str` default always asks.
    """


@dataclass
class ReplayStats:
    """Counters for a `silent_replay` run."""

    auto: int = 0
    asked: int = 0


# One flat module variable, no stack: nested silent_replay() calls are not supported.
_active_stats: ReplayStats | None = None
_header_shown = False


def in_silent_replay() -> bool:
    """True while a `silent_replay` context is active."""
    return _active_stats is not None


@contextmanager
def silent_replay():
    """Activate silent-replay mode: auto-accept recovered defaults, ask the rest.

    Yields the `ReplayStats` for this run. Deactivates on exit, including on
    an exception, so a failure never leaves the module stuck in silent mode.
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
    """Temporarily turn off silent mode inside an active `silent_replay`.

    Use around a fresh provider's sub-questionnaire, which must ask every
    question regardless of the outer replay. A no-op when silent mode is
    not active.
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
    global _header_shown
    if not _header_shown:
        ui.info("This release asks about new settings:")
        _header_shown = True


def _require(text) -> bool | str:
    """Reject an empty or whitespace-only answer."""
    return True if (text or "").strip() else "A value is required."


def _text_question(
    prompt: str,
    default: str = "",
    required: bool = True,
    placeholder: str | None = None,
    validate: Callable[[str], bool | str] | None = None,
):
    """Build a questionary.text Question without prompting.

    A non-empty `default` wins over `placeholder` and renders as an editable
    pre-fill; `placeholder` alone renders as a ghost hint the operator must
    type over.
    """
    validator = validate or (_require if required else None)
    if default:
        # native editable pre-fill, no custom styling; validate honours `required`.
        return questionary.text(prompt, default=default, validate=validator)
    if placeholder is not None:
        # Explicit dim grey on the fragment itself: questionary's default style
        # leaves `class:placeholder` near-white, so the colour is set inline to
        # read as a hint regardless of the active style sheet.
        return questionary.text(
            prompt,
            placeholder=FormattedText([(_PLACEHOLDER_STYLE, placeholder)]),
            validate=validator,
        )
    return questionary.text(prompt, default="", validate=validator)


def _ask_or_abort(question) -> str:
    """Ask `question`, announcing the silent-mode header first when active.

    Raises StCliError on cancel (Esc/Ctrl+C) instead of returning None.
    """
    if _active_stats is not None:
        _announce_silent_question()
        _active_stats.asked += 1
    ans = question.ask()
    if ans is None:
        raise StCliError("bootstrap cancelled by user.")
    return ans


def _ask(
    prompt: str,
    default: str = "",
    required: bool = True,
    placeholder: str | None = None,
    validate: Callable[[str], bool | str] | None = None,
) -> str:
    """Ask a free-text question; pass required=False for an optional field.

    In silent-replay mode, a non-empty `Recovered` default auto-accepts, and
    any default auto-accepts when required=False; every other case still asks.
    """
    if _active_stats is not None and (
        (isinstance(default, Recovered) and default.strip()) or not required
    ):
        _active_stats.auto += 1
        return str(default).strip()
    question = _text_question(
        prompt,
        default=default,
        required=required,
        placeholder=placeholder,
        validate=validate,
    )
    return _ask_or_abort(question).strip()


def _password(prompt: str, required: bool = True) -> str:
    """Ask a hidden-input question; never auto-accepts in silent-replay mode."""
    question = questionary.password(prompt, validate=_require if required else None)
    return _ask_or_abort(question)


def _confirm(prompt: str, default: bool = False, auto: bool = True) -> bool:
    """Yes/no confirmation; raises StCliError on cancel.

    In silent-replay mode, returns `default` without asking when `auto` is
    True; pass `auto=False` for a genuinely new question.
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
    """True if `h` is a valid IP address or hostname.

    A string that looks like an IP (contains `:`, or is dotted-numeric) must
    parse as a real IP, so a typo like `10.1.1.a` is rejected instead of
    accepted as a technically-legal hostname.
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


def _split_hosts(raw: str) -> list[str]:
    return [h.strip() for h in raw.split(",") if h.strip()]


def _ask_hosts(
    label: str, allow_empty: bool = False, default: list[str] | None = None
) -> list[str]:
    """Prompt for comma-separated hosts, IP or hostname, each validated.

    `allow_empty` accepts a blank answer as `[]`. In silent-replay mode, a
    non-empty `default` auto-accepts; an empty one auto-accepts as `[]`
    only when `allow_empty`.
    """
    if _active_stats is not None:
        if default:
            _active_stats.auto += 1
            return list(default)
        if allow_empty:
            _active_stats.auto += 1
            return []

    def validate(raw: str):
        hosts = _split_hosts(raw)
        if not hosts:
            if allow_empty:
                return True
            return "Enter at least one host (IP address or hostname)."
        bad = [h for h in hosts if not _is_valid_host(h)]
        if bad:
            return "Not a valid IP/hostname: " + ", ".join(bad)
        return True

    question = questionary.text(
        f"{label} host(s) — IP or hostname, comma-separated",
        default=", ".join(default) if default else "",
        validate=validate,
    )
    return _split_hosts(_ask_or_abort(question))


def _ask_select(
    message: str, choices: list[str], default: str | None = None, auto: bool = True
) -> str:
    """Single-choice select; raises StCliError on cancel.

    A `default` not present in `choices` is silently omitted rather than
    raising. In silent-replay mode with `auto` True, a valid default
    auto-accepts; otherwise this still asks.
    """
    if _active_stats is not None and auto and default and default in choices:
        _active_stats.auto += 1
        return default
    kwargs = {}
    if default and default in choices:
        kwargs["default"] = default
    question = questionary.select(message, choices=choices, **kwargs)
    return _ask_or_abort(question)


def _confirm_ready(message: str) -> None:
    """Yes/no readiness gate; declining raises StCliError instead of continuing."""
    if not _confirm(message, default=True):
        raise StCliError(
            "bootstrap cancelled — prepare the requirements above, then re-run."
        )
