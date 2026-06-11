"""CLI argument parsing shared across Twenty Questions framework implementations."""

import argparse
from datetime import UTC, datetime
from pathlib import Path

DEFAULT_MODELS: dict[str, str] = {"openai": "gpt-4o-mini", "anthropic": "claude-haiku-4-5-20251001"}


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--variant", choices=["states", "wide"], required=True)
    parser.add_argument("--model", default=None, help="Model name (default depends on --api)")
    parser.add_argument("--api", choices=["openai", "anthropic"], default="openai", help="API provider")
    parser.add_argument("--output-dir", default=None, help="Output directory")


def resolve_args(args: argparse.Namespace) -> None:
    """Fill in defaults that depend on other arguments."""
    if args.model is None:
        args.model = DEFAULT_MODELS[args.api]


def output_dir_from_args(args: argparse.Namespace) -> Path:
    d = Path(args.output_dir) if args.output_dir else Path("eval_results") / datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    d.mkdir(parents=True, exist_ok=True)
    return d
