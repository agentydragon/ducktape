"""Render both staging policy catalogs as a cacheable test input."""

import argparse
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from cdk8s import App

from cluster.cdk8s.agentplane import staging
from cluster.cdk8s.agentplane.environment import Environment


def _synth(env: Environment, directory: Path) -> list[dict[str, Any]]:
    # Both chart functions use the id "agentplane", so they need separate apps.
    return (
        staging.chart(App(outdir=str(directory / "app")), env).to_json()
        + staging.delivery_chart(App(outdir=str(directory / "delivery")), env).to_json()
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(dir=args.output.parent) as directory:
        root = Path(directory)
        catalogs = {
            "staging": _synth(staging.ENV, root / "staging"),
            "identity_catalog": _synth(staging.environment(identity_catalog=True), root / "identity"),
        }
    args.output.write_text(json.dumps(catalogs))


if __name__ == "__main__":
    main()
