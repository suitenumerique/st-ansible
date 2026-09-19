"""Tests for st_cli.core.pin — installed-CLI-vs-.st-cli.yml-pin comparison."""

from __future__ import annotations

import pytest

import st_cli
from st_cli.core import pin
from st_cli.core.errors import StCliError
from st_cli.core.models import StCliManifest


def _manifest(cli_version: str) -> StCliManifest:
    return StCliManifest("0.0.19", cli_version, [])


def test_compare_aligned(monkeypatch):
    monkeypatch.setattr(st_cli, "__version__", "0.3.1")
    assert pin.compare(_manifest("0.3.1")) is pin.PinState.ALIGNED


def test_compare_cli_older(monkeypatch):
    monkeypatch.setattr(st_cli, "__version__", "0.3.0")
    assert pin.compare(_manifest("0.4.0")) is pin.PinState.CLI_OLDER


def test_compare_cli_newer(monkeypatch):
    monkeypatch.setattr(st_cli, "__version__", "0.4.0")
    assert pin.compare(_manifest("0.3.0")) is pin.PinState.CLI_NEWER


def test_compare_empty_pin_is_unknown(monkeypatch):
    monkeypatch.setattr(st_cli, "__version__", "0.3.1")
    assert pin.compare(_manifest("")) is pin.PinState.UNKNOWN


def test_compare_unparsable_pin_is_unknown(monkeypatch):
    monkeypatch.setattr(st_cli, "__version__", "0.3.1")
    assert pin.compare(_manifest("not-a-version")) is pin.PinState.UNKNOWN


def test_compare_unparsable_installed_is_unknown(monkeypatch):
    monkeypatch.setattr(st_cli, "__version__", "not-a-version")
    assert pin.compare(_manifest("0.3.1")) is pin.PinState.UNKNOWN


def test_compare_real_zero_pin_is_not_unknown(monkeypatch):
    """A pin of `0.0.0` is a real version, not the tolerant parser's garbage
    marker — it must compare normally, not read as UNKNOWN."""
    monkeypatch.setattr(st_cli, "__version__", "0.3.1")
    assert pin.compare(_manifest("0.0.0")) is pin.PinState.CLI_NEWER


def test_require_not_older_raises_with_retry_hint(monkeypatch):
    """require_not_older raises, naming the pin and ending on the retry hint."""
    monkeypatch.setattr(st_cli, "__version__", "0.3.0")
    with pytest.raises(StCliError) as exc_info:
        pin.require_not_older(_manifest("0.4.0"), "then retry.")
    msg = str(exc_info.value)
    assert "st-cli 0.3.0 is older than the .st-cli.yml pin 0.4.0" in msg
    assert msg.endswith("then retry.")


def test_require_not_older_is_a_no_op_when_not_older(monkeypatch):
    """require_not_older does nothing when the installed CLI is not older."""
    monkeypatch.setattr(st_cli, "__version__", "0.4.0")
    pin.require_not_older(_manifest("0.3.0"), "then retry.")
