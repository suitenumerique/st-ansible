"""Text-level merge primitive for dotenv-style env blobs.

`merge` folds a fresh Jinja render back into an operator's committed blob for
a rebootstrap: it keeps existing lines in place, appends new keys under a
marker, and never deletes a line. Pure text in, text out; no disk I/O.
"""

from __future__ import annotations

import re

_LINE_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


def parse(text: str) -> dict[str, str]:
    """Parse a dotenv-style blob into `{KEY: value}`.

    The value is everything after the first `=`, taken verbatim. On a
    duplicate key, the last occurrence wins.
    """
    out: dict[str, str] = {}
    for line in text.splitlines():
        m = _LINE_RE.match(line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def keys(text: str) -> list[str]:
    """Return the keys in file order. The first occurrence of a duplicate wins.

    `parse` keeps the last value of a duplicate; `merge` rewrites its first line.
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
    """Fold a fresh `rendered` blob into an operator's `existing` one.

    A shared key keeps `existing`'s position and takes `rendered`'s value.
    Every other committed line stays verbatim. A new key appends after one
    `marker` line. `merge(x, x, m) == x` when `x` ends with one newline.
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
            out_lines.append(line)
            continue
        key = m.group(1)
        if key in seen_keys:
            out_lines.append(line)
            continue
        seen_keys.add(key)
        if key in rendered_map:
            out_lines.append(f"{key}={rendered_map[key]}")
        else:
            out_lines.append(line)

    new_keys = [k for k in keys(rendered) if k not in seen_keys]
    if new_keys:
        out_lines.append(marker)
        out_lines += [f"{key}={rendered_map[key]}" for key in new_keys]

    return "\n".join(out_lines) + "\n"
