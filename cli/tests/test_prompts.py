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


# --------------------------------------------------------------------------
# silent_replay
# --------------------------------------------------------------------------


def test_ask_auto_accepts_nonempty_recovered_default_without_prompting(spies):
    text_spy, _ = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask("Host", default=prompts.Recovered("db.example.org"))

    assert result == "db.example.org"
    assert text_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_ask_plain_default_still_prompts_and_shows_header_once(spies, mocker):
    text_spy, _ = spies
    text_spy.answer = "5432"
    info = mocker.patch.object(prompts.ui, "info")

    with prompts.silent_replay() as stats:
        first = prompts._ask("DB_PORT", default="5432")
        second = prompts._ask("DB_TIMEOUT", default="30")

    assert first == "5432"
    assert second == "5432"  # spy always answers "5432" regardless of prompt
    assert len(text_spy.calls) == 2
    assert stats.asked == 2
    assert stats.auto == 0
    info.assert_called_once_with("This release asks about new settings:")


def test_ask_empty_default_prompts_in_silent_mode(spies):
    text_spy, _ = spies
    text_spy.answer = "typed-value"

    with prompts.silent_replay() as stats:
        result = prompts._ask("New setting")

    assert result == "typed-value"
    assert len(text_spy.calls) == 1
    assert stats.asked == 1
    assert stats.auto == 0


def test_ask_optional_empty_default_auto_accepts_in_silent_mode(spies):
    text_spy, _ = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask("AWS_S3_REGION_NAME", default="", required=False)

    assert result == ""
    assert text_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_ask_optional_plain_default_auto_accepts_in_silent_mode(spies):
    text_spy, _ = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask("Region", default="eu-west-1", required=False)

    assert result == "eu-west-1"
    assert text_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_ask_optional_recovered_default_auto_accepts_in_silent_mode(spies):
    text_spy, _ = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask(
            "Region", default=prompts.Recovered("eu-west-3"), required=False
        )

    assert result == "eu-west-3"
    assert text_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_confirm_returns_default_in_silent_mode_without_prompting(monkeypatch):
    confirm_spy = _Spy()
    monkeypatch.setattr(prompts.questionary, "confirm", confirm_spy)

    with prompts.silent_replay() as stats:
        result = prompts._confirm("Enable feature?", default=True)

    assert result is True
    assert confirm_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_confirm_auto_false_prompts_in_silent_mode(monkeypatch):
    confirm_spy = _Spy()
    confirm_spy.answer = False
    monkeypatch.setattr(prompts.questionary, "confirm", confirm_spy)

    with prompts.silent_replay() as stats:
        result = prompts._confirm(
            "Really destroy everything?", default=False, auto=False
        )

    assert result is False
    assert len(confirm_spy.calls) == 1
    assert stats.asked == 1
    assert stats.auto == 0


def test_ask_select_auto_accepts_default_present_in_choices(spies):
    _, select_spy = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask_select("Pick one:", ["a", "b", "c"], default="b")

    assert result == "b"
    assert select_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_ask_select_prompts_when_default_none_in_silent_mode(spies):
    _, select_spy = spies
    select_spy.answer = "a"

    with prompts.silent_replay() as stats:
        result = prompts._ask_select("Pick one:", ["a", "b"])

    assert result == "a"
    assert len(select_spy.calls) == 1
    assert stats.asked == 1


def test_ask_select_prompts_when_default_not_in_choices_in_silent_mode(spies):
    _, select_spy = spies
    select_spy.answer = "a"

    with prompts.silent_replay() as stats:
        result = prompts._ask_select("Pick one:", ["a", "b"], default="stale")

    assert result == "a"
    assert len(select_spy.calls) == 1
    assert stats.asked == 1


def test_ask_hosts_auto_accepts_recovered_default_in_silent_mode(spies):
    text_spy, _ = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask_hosts("meet", default=["10.0.0.1"])

    assert result == ["10.0.0.1"]
    assert text_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_ask_hosts_empty_default_allow_empty_auto_returns_empty_list(spies):
    text_spy, _ = spies

    with prompts.silent_replay() as stats:
        result = prompts._ask_hosts("workers", allow_empty=True)

    assert result == []
    assert text_spy.calls == []
    assert stats.auto == 1
    assert stats.asked == 0


def test_ask_hosts_empty_default_required_prompts_in_silent_mode(spies):
    text_spy, _ = spies
    text_spy.answer = "10.0.0.9"

    with prompts.silent_replay() as stats:
        result = prompts._ask_hosts("meet")

    assert result == ["10.0.0.9"]
    assert len(text_spy.calls) == 1
    assert stats.asked == 1
    assert stats.auto == 0


def test_password_always_prompts_in_silent_mode(monkeypatch):
    password_spy = _Spy()
    password_spy.answer = "s3cret"
    monkeypatch.setattr(prompts.questionary, "password", password_spy)

    with prompts.silent_replay() as stats:
        result = prompts._password("New API key")

    assert result == "s3cret"
    assert len(password_spy.calls) == 1
    assert stats.asked == 1
    assert stats.auto == 0


def test_suspend_silent_prompts_normally_and_counts_nothing(spies):
    text_spy, _ = spies
    text_spy.answer = "typed"

    with prompts.silent_replay() as stats:
        with prompts.suspend_silent():
            assert prompts.in_silent_replay() is False
            result = prompts._ask("Fresh provider setting", default="fallback")
        assert prompts.in_silent_replay() is True

    assert result == "typed"
    assert len(text_spy.calls) == 1
    assert stats.auto == 0
    assert stats.asked == 0


def test_suspend_silent_is_noop_outside_silent_mode(spies):
    text_spy, _ = spies
    text_spy.answer = "typed"

    assert prompts.in_silent_replay() is False
    with prompts.suspend_silent():
        assert prompts.in_silent_replay() is False
        result = prompts._ask("Some setting", default="fallback")
    assert prompts.in_silent_replay() is False

    assert result == "typed"


def test_in_silent_replay_false_outside_true_inside():
    assert prompts.in_silent_replay() is False
    with prompts.silent_replay():
        assert prompts.in_silent_replay() is True
    assert prompts.in_silent_replay() is False


def test_in_silent_replay_false_after_exception_escapes():
    with pytest.raises(RuntimeError):
        with prompts.silent_replay():
            assert prompts.in_silent_replay() is True
            raise RuntimeError("boom")
    assert prompts.in_silent_replay() is False


def test_interactive_recovered_default_behaves_like_plain_str(spies):
    """Outside silent mode, a Recovered default is just a str: it still prompts."""
    text_spy, _ = spies
    text_spy.answer = "edited-value"

    result = prompts._ask("Host", default=prompts.Recovered("db.example.org"))

    assert result == "edited-value"
    assert len(text_spy.calls) == 1
    assert text_spy.kwargs["default"] == "db.example.org"
