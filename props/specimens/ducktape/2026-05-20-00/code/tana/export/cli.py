"""Command-line entry points for the Tana export toolkit."""

from __future__ import annotations

from tana.export.convert import main as convert_main


def main() -> int:
    """Invoke the default export CLI."""
    convert_main()
    return 0
