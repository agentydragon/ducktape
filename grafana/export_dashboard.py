"""Serialize one Foundation SDK dashboard as a Bazel build output."""

import argparse
from collections.abc import Callable
from pathlib import Path

from grafana_foundation_sdk.cog.encoder import JSONEncoder
from grafana_foundation_sdk.models.dashboard import Dashboard


def main(build_dashboard: Callable[[], Dashboard]) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    output = parser.parse_args().output
    output.write_text(JSONEncoder().encode(build_dashboard()))
