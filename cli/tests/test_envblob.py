"""Tests for st_cli.core.envblob — the env-blob text merge primitive."""

from __future__ import annotations

from st_cli.core import envblob

MARKER = "# added by st-cli 0.3.0"


# --------------------------------------------------------------------------- parse


def test_parse_value_contains_equals():
    text = "DATABASE_URL=postgres://user:pw@host/db?opt=1\n"
    assert envblob.parse(text) == {"DATABASE_URL": "postgres://user:pw@host/db?opt=1"}


def test_parse_keeps_jinja_verbatim():
    text = (
        "DJANGO_SECRET_KEY={{ vault_django_secret_key }}\n"
        "DATABASE_URL={{ lookup('community.hashi_vault.hashi_vault', 'kv/data/db:url') }}\n"
    )
    parsed = envblob.parse(text)
    assert parsed["DJANGO_SECRET_KEY"] == "{{ vault_django_secret_key }}"
    assert (
        parsed["DATABASE_URL"]
        == "{{ lookup('community.hashi_vault.hashi_vault', 'kv/data/db:url') }}"
    )


def test_parse_ignores_comments_and_blanks():
    text = "# a comment\n\nKEY=value\n   \n"
    assert envblob.parse(text) == {"KEY": "value"}


def test_parse_duplicate_key_last_wins():
    text = "KEY=first\nKEY=second\n"
    assert envblob.parse(text) == {"KEY": "second"}


def test_parse_ignores_invalid_key_lines():
    # lowercase-leading-digit / indented / malformed lines are not valid keys
    text = "1KEY=value\n  INDENTED=value\nNOEQUALS\n"
    assert envblob.parse(text) == {}


# --------------------------------------------------------------------------- keys


def test_keys_in_file_order_first_occurrence():
    text = "B=1\nA=2\nB=3\n"
    assert envblob.keys(text) == ["B", "A"]


# --------------------------------------------------------------------------- merge


def test_merge_idempotent():
    x = "DJANGO_SETTINGS_MODULE=meet.settings\nDJANGO_SECRET_KEY={{ vault_django_secret_key }}\n# an operator's own comment\nMY_CUSTOM_VAR=1\n"
    assert envblob.merge(x, x, MARKER) == x


def test_merge_fixed_point_reordered_rendered():
    existing = "A=1\nB=2\nC=3\n"
    # same keys/values, different order -> position must come from `existing`
    rendered = "C=3\nA=1\nB=2\n"
    assert envblob.merge(existing, rendered, MARKER) == existing


def test_merge_preserves_operator_custom_var_and_comment():
    existing = (
        "DJANGO_SETTINGS_MODULE=meet.settings\n"
        "# an operator's own comment\n"
        "MY_CUSTOM_VAR=1\n"
    )
    rendered = "DJANGO_SETTINGS_MODULE=meet.settings\n"
    result = envblob.merge(existing, rendered, MARKER)
    assert result == existing


def test_merge_changed_value_replaces_in_place():
    existing = "A=1\nB=2\nC=3\n"
    rendered = "A=1\nB=CHANGED\nC=3\n"
    result = envblob.merge(existing, rendered, MARKER)
    assert result == "A=1\nB=CHANGED\nC=3\n"
    # position unchanged: B stays on line 2
    assert result.splitlines()[1] == "B=CHANGED"


def test_merge_appends_new_keys_once_after_marker_in_rendered_order():
    existing = "A=1\n"
    rendered = "A=1\nNEW_B=2\nNEW_C=3\n"
    result = envblob.merge(existing, rendered, MARKER)
    assert result == f"A=1\n{MARKER}\nNEW_B=2\nNEW_C=3\n"


def test_merge_no_new_keys_no_marker():
    existing = "A=1\n"
    rendered = "A=1\n"
    result = envblob.merge(existing, rendered, MARKER)
    assert MARKER not in result
    assert result == "A=1\n"


def test_merge_empty_existing_returns_rendered():
    rendered = "A=1\nB=2\n"
    assert envblob.merge("", rendered, MARKER) == rendered
    assert envblob.merge("   \n  \n", rendered, MARKER) == rendered


def test_merge_duplicate_key_in_existing_only_first_rewritten():
    existing = "KEY=old1\nOTHER=x\nKEY=old2\n"
    rendered = "KEY=new\n"
    result = envblob.merge(existing, rendered, MARKER)
    assert result == "KEY=new\nOTHER=x\nKEY=old2\n"


def test_merge_normalises_trailing_newline():
    existing = "A=1"  # no trailing newline
    rendered = "A=1\nB=2\n"
    result = envblob.merge(existing, rendered, MARKER)
    assert result.endswith("\n") and not result.endswith("\n\n")
    assert result == f"A=1\n{MARKER}\nB=2\n"
