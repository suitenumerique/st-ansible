"""Unit tests for core/prompts.py's rebootstrap ``default`` pre-fill additions.

``_ask_hosts`` and ``_ask_select`` gained a ``default`` kwarg so a rebootstrap
run can pre-fill every questionnaire answer from the operator's current
config. These primitives are otherwise only exercised indirectly, via
``test_bootstrap.py``'s ``ScriptedQuestionary`` (see ``tests/helpers.py``),
which asserts on the canned *answer* it returns but never on the kwargs
questionary was called with. Here a lightweight spy replaces
``questionary.text``/``questionary.select`` directly, recording ``(args,
kwargs)`` per call and returning a stand-in whose ``.ask()`` replays a canned
answer — so we can assert exactly what st-cli asked questionary for.
"""

from __future__ import annotations

import pytest

from st_cli.core import prompts
from st_cli.core.errors import StCliError


class _Answer:
    """Stand-in for a questionary ``Question``: `.ask()` replays a canned answer."""

    def __init__(self, answer):
        self._answer = answer

    def ask(self):
        return self._answer


class _Spy:
    """Replaces a ``questionary.*`` factory (``text``/``select``).

    Records every call's ``(args, kwargs)`` and returns a ``_Answer`` wrapping
    ``self.answer`` (set by the test right before invoking the prompt helper).
    """

    def __init__(self):
        self.answer = None
        self.calls: list[tuple[tuple, dict]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _Answer(self.answer)

    @property
    def kwargs(self) -> dict:
        """kwargs of the most recent call."""
        return self.calls[-1][1]


@pytest.fixture
def spies(monkeypatch):
    """Patch ``prompts.questionary.text``/``.select`` with independent spies."""
    text_spy = _Spy()
    select_spy = _Spy()
    monkeypatch.setattr(prompts.questionary, "text", text_spy)
    monkeypatch.setattr(prompts.questionary, "select", select_spy)
    return text_spy, select_spy


# --------------------------------------------------------------------------
# _ask_hosts
# --------------------------------------------------------------------------


def test_ask_hosts_default_prefills_comma_joined_and_roundtrips(spies):
    text_spy, _ = spies
    text_spy.answer = "10.0.0.1, 10.0.0.2"

    result = prompts._ask_hosts("meet", default=["10.0.0.1", "10.0.0.2"])

    assert result == ["10.0.0.1", "10.0.0.2"]
    assert text_spy.kwargs["default"] == "10.0.0.1, 10.0.0.2"


def test_ask_hosts_no_default_omits_prefill(spies):
    """Today's behaviour (no ``default``) must be preserved exactly."""
    text_spy, _ = spies
    text_spy.answer = "10.0.0.1"

    prompts._ask_hosts("meet")

    # Either omitted entirely or passed as "" — both are "no pre-fill".
    assert text_spy.kwargs.get("default", "") == ""


def test_ask_hosts_empty_list_default_also_omits_prefill(spies):
    text_spy, _ = spies
    text_spy.answer = "10.0.0.1"

    prompts._ask_hosts("meet", default=[])

    assert text_spy.kwargs.get("default", "") == ""


def test_ask_hosts_validation_rejects_invalid_host(spies):
    text_spy, _ = spies
    text_spy.answer = "10.0.0.1"

    prompts._ask_hosts("meet", default=["10.0.0.1"])
    validate = text_spy.kwargs["validate"]

    assert validate("10.0.0.1") is True
    assert isinstance(validate("not a host!!"), str)
    assert isinstance(validate(""), str)  # allow_empty defaults to False


def test_ask_hosts_validation_honours_allow_empty(spies):
    text_spy, _ = spies
    text_spy.answer = ""

    prompts._ask_hosts("meet", allow_empty=True)
    validate = text_spy.kwargs["validate"]

    assert validate("") is True


def test_ask_hosts_cancel_raises(spies):
    text_spy, _ = spies
    text_spy.answer = None

    with pytest.raises(StCliError):
        prompts._ask_hosts("meet", default=["10.0.0.1"])


# --------------------------------------------------------------------------
# _ask_select
# --------------------------------------------------------------------------


def test_ask_select_default_in_choices_is_passed_through(spies):
    _, select_spy = spies
    select_spy.answer = "b"

    result = prompts._ask_select("Pick one:", ["a", "b", "c"], default="b")

    assert result == "b"
    assert select_spy.kwargs["default"] == "b"


def test_ask_select_default_not_in_choices_is_omitted(spies):
    _, select_spy = spies
    select_spy.answer = "a"

    result = prompts._ask_select("Pick one:", ["a", "b"], default="stale-choice")

    assert result == "a"
    assert "default" not in select_spy.kwargs


def test_ask_select_default_none_is_omitted(spies):
    _, select_spy = spies
    select_spy.answer = "a"

    prompts._ask_select("Pick one:", ["a", "b"])

    assert "default" not in select_spy.kwargs


def test_ask_select_cancel_raises(spies):
    _, select_spy = spies
    select_spy.answer = None

    with pytest.raises(StCliError):
        prompts._ask_select("Pick one:", ["a", "b"])
