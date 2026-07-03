"""Exception types for st-cli."""

from __future__ import annotations


class StCliError(Exception):
    """Base class for all expected st-cli failures.

    ``main.py`` catches this and prints a clean message instead of a traceback.
    """
