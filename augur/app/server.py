"""Bazel-runnable public Augur server binary."""

from __future__ import annotations

import argparse
from pathlib import Path

from augur.app.config import load_augur_config, resolve_augur_config_path
from augur.app.http_server import StaticBundle, run_server
from util.bazel.runfiles import get_required_path


def _split_config_arg(argv: list[str] | None) -> tuple[Path, list[str]]:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--config", help="Path to AugurConfig YAML. Defaults to $AUGUR_CONFIG_PATH or /etc/augur/config.yaml."
    )
    args, remaining = parser.parse_known_args(argv)
    return (Path(args.config).resolve() if args.config else resolve_augur_config_path()), remaining


def main(argv: list[str] | None = None) -> int:
    config_path, remaining = _split_config_arg(argv)
    dist_dir = get_required_path("_main/augur/app/dist/index.html").parent
    market_config_path = get_required_path("_main/augur/model/config/market_config.example.json")
    return run_server(
        augur_config=load_augur_config(config_path),
        bundle=StaticBundle(dist_dir=dist_dir),
        default_market_config_path=market_config_path,
        argv=remaining,
    )


if __name__ == "__main__":
    raise SystemExit(main())
