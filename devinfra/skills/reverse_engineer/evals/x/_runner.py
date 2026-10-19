"""Shared CLI plumbing for the eval (`run.py`) and the judge validation
runs (`validate_judge.py`).

Concentrates: credential validation, log-dir defaulting, the
`inspect_eval()` invocation. The two entry points differ only in which
`@task` they construct and what flags they expose.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path

from inspect_ai import Task, eval as inspect_eval

logger = logging.getLogger(__name__)


# Provider prefix → required environment variable. Mirrors Inspect's own
# provider model-string parsing — the prefix before the first `/` selects
# the provider.
_PROVIDER_API_KEY_ENV: dict[str, str] = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-api": "OPENAI_API_KEY",
}


def validate_credentials(model: str) -> None:
    provider = model.split("/", 1)[0]
    if provider == "openai-api":
        # Inspect's openai-api provider validates credentials internally
        # (derives <SERVICE>_API_KEY from the service segment). Skip our
        # check — Inspect's error message is clear enough.
        return
    if (env_var := _PROVIDER_API_KEY_ENV.get(provider)) and not os.environ.get(env_var):
        sys.exit(f"{env_var} is not set; refusing to run {model!r}.")


def default_log_dir(*, subdir: str) -> Path:
    """Timestamped log dir under the user's invocation CWD.

    `bazel run` sets `BUILD_WORKING_DIRECTORY` to the directory the user
    launched from; we prefer that over `Path.cwd()` because under Bazel
    the cwd is the runfiles tree (mostly read-only and not easy to
    discover). Falls back to `Path.cwd()` outside Bazel.
    """
    stamp = dt.datetime.now(tz=dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    base = Path(os.environ.get("BUILD_WORKING_DIRECTORY") or Path.cwd())
    return base / subdir / stamp


def add_common_flags(parser: argparse.ArgumentParser, *, default_model: str) -> None:
    """Flags shared by run.py and validate_judge.py."""
    parser.add_argument("--model", default=default_model)
    parser.add_argument("--log-dir", type=Path, default=None)
    parser.add_argument(
        "--display",
        choices=["full", "conversation", "rich", "plain", "log", "none"],
        default="plain",
        help="Inspect display mode. `plain` is friendlier under bazel run; "
        "switch to `conversation` to watch the conversation in realtime.",
    )


def run_eval(
    *,
    args: argparse.Namespace,
    log_subdir: str,
    task_factory: Callable[[], Task],
    pre_eval: Callable[[Path], None] | None = None,
) -> None:
    """Boilerplate around `inspect_eval()` shared by both CLIs.

    `pre_eval` is called with the resolved log_dir before the eval runs —
    used by run.py to stamp `RE_EVAL_SNAPSHOT_DIR`. validate_judge.py
    has nothing to set there.
    """
    validate_credentials(args.model)
    log_dir = args.log_dir or default_log_dir(subdir=log_subdir)
    log_dir.mkdir(parents=True, exist_ok=True)

    if pre_eval is not None:
        pre_eval(log_dir)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logger.info("Inspect log dir: %s", log_dir)

    inspect_eval(task_factory(), model=args.model, log_dir=str(log_dir), display=args.display)
