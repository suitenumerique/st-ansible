"""Text-level merge primitive for dotenv-style env blobs.

An app component's ``vars.yml`` embeds one or more ``st_<app>_*_env`` literal
block scalars — plain ``KEY=value`` bodies (see :mod:`envrender`) that an
operator is free to hand-edit after bootstrap (add their own vars, comment
things, reorder). The "rebootstrap" flow re-renders those bodies from the
current Jinja templates and must fold the fresh render back into the
operator's blob **without ever discarding what they added by hand**.

This module holds the pure, dependency-free text functions that make that
possible: :func:`parse` and :func:`keys` recover structure from a blob (the
same blob is later fed back into the renderer as input, e.g. to seed
previously-answered values), and :func:`merge` performs the actual fold.

Nothing here touches disk or imports another ``st_cli`` module — it operates
on strings in, strings out, so it can be unit-tested in isolation from the
YAML tree, Jinja rendering and vault machinery that surround it.
"""

from __future__ import annotations

import re

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse(text: str) -> dict[str, str]:
    """Parse a dotenv-style blob into ``{KEY: value}``.

    A line matches when it starts with a valid shell-style identifier
    (``[A-Za-z_][A-Za-z0-9_]*``) followed by ``=``; the value is **everything
    after the first `=`, taken verbatim** — it may itself contain ``=`` (e.g. a
    base64 blob) or Jinja (``{{ vault_x }}``, ``{{ lookup('kv/data/db:url') }}``
    — note the colon and inner quoting are never touched). Values are never
    stripped, unquoted or otherwise interpreted.

    Non-matching lines (comments, blank lines, indented or otherwise malformed
    lines) are silently ignored — they carry no key.

    On a duplicate key, **the last occurrence wins**: this mirrors how a shell
    sourcing the same file would behave (later assignments overwrite earlier
    ones), and matters for callers that recover *values* to seed a re-render.
    For callers that care about *position* instead, see :func:`keys`.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def keys(text: str) -> list[str]:
    """Return the blob's keys in file order, deduplicated on first occurrence.

    This is the position counterpart to :func:`parse`: where ``parse`` resolves
    a duplicate key to its *last* value (shell-sourcing semantics), ``keys``
    reports the key at the position of its *first* line — which is exactly
    where :func:`merge` rewrites a duplicate key's first occurrence in place
    and leaves any later occurrence untouched. The two are deliberately
    answering different questions (value vs. position); neither is "more
    correct" than the other.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            key = m.group(1)
            if key not in seen:
                seen.add(key)
                out.append(key)
    return out


def merge(existing: str, rendered: str, marker: str) -> str:
    """Fold a fresh ``rendered`` blob into an operator's ``existing`` one.

    This is the primitive the rebootstrap flow is built on: it re-renders a
    component's env blob from the current Jinja templates, then calls this to
    reconcile the result with whatever is already committed, so a rerun is
    never destructive. The rules, in order of precedence:

    * A key present in **both** -> the existing line's *position* wins, the
      *rendered* line's content wins (upstream default/format changes land,
      an operator's custom value would be overwritten by design — the
      operator-only keys below are what actually survive untouched).
    * A key present only in ``existing`` -> kept **verbatim, in place** (an
      operator's own addition — never dropped).
    * A non-key line in ``existing`` (comment, blank, stray text) -> kept
      **verbatim, in place**.
    * A key present only in ``rendered`` (new upstream key) -> **appended at
      the end**, in rendered order, preceded by a single ``marker`` line. The
      marker is emitted only when at least one key is actually appended, so a
      rerun that introduces nothing new leaves the tail untouched.
    * **Nothing already committed is ever deleted.**
    * A duplicate key in ``existing``: only the **first** occurrence is
      rewritten (that is where :func:`keys` places it); any later occurrence
      of the same key is left exactly as-is. We do not attempt to guess which
      duplicate the operator "meant" — first-in-file is the same tie-break
      :func:`keys` uses, so the two stay consistent.

    When ``existing`` is empty or whitespace-only, this is a first-ever write:
    ``rendered`` is returned unchanged (there is nothing to preserve yet).

    Otherwise the result always ends with **exactly one** trailing newline,
    regardless of whether ``existing`` had one. Every line that is "kept" is
    reused byte-for-byte from ``existing`` — no trimming, no whitespace
    normalisation — which is what makes ``merge(x, x, m) == x`` and, more
    importantly, makes the operation a fixed point: since the rebootstrap flow
    recovers its render inputs *from* this same blob, re-rendering unchanged
    answers and merging back must reproduce ``existing`` exactly, or every
    rebootstrap would introduce spurious churn.
    """
    if not existing.strip():
        return rendered

    rendered_map = parse(rendered)
    existing_lines = existing.splitlines()

    out_lines: list[str] = []
    seen_keys: set[str] = set()
    for line in existing_lines:
        m = _LINE_RE.match(line)
        if not m:
            out_lines.append(line)  # comment/blank/stray — verbatim, in place
            continue
        key = m.group(1)
        if key in seen_keys:
            out_lines.append(line)  # later duplicate — untouched
            continue
        seen_keys.add(key)
        if key in rendered_map:
            out_lines.append(f"{key}={rendered_map[key]}")
        else:
            out_lines.append(line)  # operator-only key — verbatim, in place

    new_keys = [k for k in keys(rendered) if k not in seen_keys]
    if new_keys:
        out_lines.append(marker)
        for key in new_keys:
            out_lines.append(f"{key}={rendered_map[key]}")

    return "\n".join(out_lines) + "\n"
