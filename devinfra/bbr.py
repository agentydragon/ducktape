"""bbr: wrapper around `bb remote` with configurable defaults.

Reads repo-level config from devinfra/bbr.json, session-level bazel flags
from $BBR_BAZELRC, and ad-hoc `bb remote` flags from $BBR_REMOTE_ARGS.
See devinfra/docs/bb_remote_internals.md for how bb remote works under the hood.
"""

import json
import os
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pygit2

_INVOCATION_ID_DIR = Path.home() / ".cache" / "bbr"
_INVOCATION_ID_FILE = _INVOCATION_ID_DIR / "last_invocation_id"

# Commits HEAD can be ahead of the likely bb-remote diff base before we warn
# that it looks stale (see check_base_branch_freshness).
_STALE_BASE_WARNING_THRESHOLD = 30

# Bazel commands recognized by bb remote (from BuildBuddy cli/parser/bazelrc).
_BAZEL_COMMANDS = frozenset(
    {
        "analyze-profile",
        "aquery",
        "build",
        "canonicalize-flags",
        "clean",
        "coverage",
        "cquery",
        "dump",
        "fetch",
        "help",
        "info",
        "license",
        "mobile-install",
        "print_action",
        "query",
        "run",
        "shutdown",
        "sync",
        "test",
        "version",
    }
)


@dataclass
class RepoConfig:
    """Repo-level bbr configuration from devinfra/bbr.json."""

    runner_exec_properties: dict[str, str] = field(default_factory=dict)
    container_image: str | None = None
    bazel_args: list[str] = field(default_factory=list)


def _read_repo_config(repo_root: Path) -> RepoConfig:
    """Read devinfra/bbr.json. Returns defaults if file is missing."""
    config_path = repo_root / "devinfra" / "bbr.json"
    if not config_path.exists():
        return RepoConfig()
    data = json.loads(config_path.read_text())
    return RepoConfig(
        runner_exec_properties=data.get("runner_exec_properties", {}),
        container_image=data.get("container_image"),
        bazel_args=data.get("bazel_args", []),
    )


def _find_bb() -> str:
    """Locate the bb binary on PATH."""
    if path := shutil.which("bb"):
        return path
    print("bbr: 'bb' not found on PATH.", file=sys.stderr)
    sys.exit(1)


def _config_get(repo: pygit2.Repository, key: str) -> str | None:
    """Read a single (layered local+global) git config value, or None if unset."""
    try:
        return repo.config[key]
    except KeyError:
        return None


def check_base_branch_freshness(repo: pygit2.Repository) -> str | None:
    """Best-effort, no-network check on bb remote's likely diff base.

    `bb remote` mirrors local git state to the runner as a base commit +
    patchset (see devinfra/docs/bb_remote_internals.md). When the current
    branch has no tracking ref on the BuildBuddy remote, it falls back to
    `<default-branch>@{upstream}` as the base — if that local tracking ref
    is stale (no recent `git fetch`), the patchset balloons and can even fail
    outright (unappliable binary patches on a huge diff). This deliberately
    never fetches — bbr running network calls on every invocation would be
    its own surprise — it only inspects locally-known refs, so a warning here
    means "you should `git fetch`", not "bbr already tried and failed".

    The default branch name comes only from `buildbuddy.remote-bazel-default-branch`,
    which `bb remote` itself detects and caches on every run — never a
    hardcoded guess. Returns a warning to print, or None if there's nothing to
    flag (including "couldn't tell" — silence, not a guess).
    """
    remote = _config_get(repo, "buildbuddy.remote-bazel-remote-name") or "origin"
    default_branch = _config_get(repo, "buildbuddy.remote-bazel-default-branch")
    if default_branch is None or repo.head_is_detached:
        return None

    current_branch = repo.head.shorthand
    current_tracking_ref = repo.references.get(f"refs/remotes/{remote}/{current_branch}")
    if current_tracking_ref is not None:
        # bb only uses HEAD directly when it's an ancestor of (or equal to) the
        # tracked commit — unpushed commits still fall through to the
        # default-branch fallback below (see bb_remote_internals.md Phase 2).
        ahead_of_own_branch, _ = repo.ahead_behind(repo.head.target, current_tracking_ref.target)
        if ahead_of_own_branch == 0:
            return None

    tracking_ref = repo.references.get(f"refs/remotes/{remote}/{default_branch}")
    if tracking_ref is None:
        return None

    ahead, _behind = repo.ahead_behind(repo.head.target, tracking_ref.target)
    if ahead <= _STALE_BASE_WARNING_THRESHOLD:
        return None

    return (
        f"bbr: HEAD is {ahead} commits ahead of {remote}/{default_branch}, "
        f"bb remote's likely diff base for this branch. If {remote}/{default_branch} "
        f"is stale, the patchset it generates may be huge or fail to apply. Consider: "
        f"git fetch {remote} {default_branch}"
    )


def _env_args(var: str) -> list[str]:
    """Parse space-separated args from an env var."""
    return shlex.split(os.environ.get(var, ""))


def _bazelrc_args() -> list[str]:
    """Read bazel flags from $BBR_BAZELRC file.

    Each non-comment, non-empty line is expected to have a command prefix
    (build, common, test, etc.) followed by a flag. The prefix is stripped
    and the flag is returned.
    """
    path_str = os.environ.get("BBR_BAZELRC")
    if not path_str:
        return []
    path = Path(path_str)
    if not path.exists():
        return []
    args: list[str] = []
    for raw_line in path.read_text().splitlines():
        stripped = raw_line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        # Strip command prefix: "build --flag" → "--flag"
        parts = stripped.split(None, 1)
        if len(parts) == 2:
            args.append(parts[1])
    return args


def find_verb_index(args: list[str]) -> int | None:
    """Find the index of the first bazel command verb in args."""
    for i, arg in enumerate(args):
        if arg in _BAZEL_COMMANDS:
            return i
    return None


def build_command(repo: pygit2.Repository, user_args: list[str]) -> list[str]:
    """Assemble the full bb remote command line.

    Argument layout:
      bb remote [bb-remote-flags] [BBR_REMOTE_ARGS] <verb> [repo bazel_args] [session bazelrc] [user flags+targets]
    """
    repo_root = Path(repo.workdir)
    config = _read_repo_config(repo_root)
    bb = _find_bb()

    _INVOCATION_ID_DIR.mkdir(parents=True, exist_ok=True)

    runner_props = [f"--runner_exec_properties={k}={v}" for k, v in config.runner_exec_properties.items()]
    container_flag = (
        [f"--container_image=docker://{config.container_image}"] if config.container_image is not None else []
    )

    # Split user_args at the bazel verb into startup options (before verb)
    # and command options (after verb).
    # Final layout: bb remote [bb-flags] [startup-opts] <verb> [repo-args] [session-args] [command-opts]
    verb_idx = find_verb_index(user_args)
    if verb_idx is not None:
        startup_options = user_args[:verb_idx]
        verb = user_args[verb_idx]
        command_options = user_args[verb_idx + 1 :]
    else:
        startup_options = user_args
        verb = None
        command_options = []

    return [
        bb,
        "remote",
        f"--invocation_id_file={_INVOCATION_ID_FILE}",
        *runner_props,
        *container_flag,
        *_env_args("BBR_REMOTE_ARGS"),
        *startup_options,
        *([verb] if verb else []),
        *config.bazel_args,
        *_bazelrc_args(),
        *command_options,
    ]


def _print_post_run_summary() -> None:
    """Print invocation ID and useful commands after bb remote completes."""
    try:
        inv_id = _INVOCATION_ID_FILE.read_text().strip()
    except OSError:
        return
    if not inv_id:
        return
    print(f'bbr: invocation {inv_id}  (bbapi {{target,"target log",artifact,invocation}} {inv_id})', file=sys.stderr)


_HELP = """\
bbr — wrapper around `bb remote` with layered configuration.

Usage: bbr [--dry-run] [--help] <bazel-verb> [flags...] [targets...]

Configuration layers (last-wins for Bazel flags):
  Repo      devinfra/bbr.json          runner properties, container image, bazel_args
  Session   $BBR_BAZELRC file          --build_metadata (ROLE, session TAGS)
  Ad-hoc    $BBR_REMOTE_ARGS env var   extra `bb remote` flags (before verb)
  CLI       user args                  flags and targets (override everything)

Flags:
  --dry-run   Print the assembled command without executing
  --help      Show this help

Environment variables:
  BBR_BAZELRC       Path to a bazelrc-format file with Bazel flags to forward.
                    Lines are parsed as "<command> <flag>" (prefix stripped).
  BBR_REMOTE_ARGS   Space-separated `bb remote` flags injected before the verb.

Examples:
  bbr test //foo:bar                          # basic test
  bbr build //foo --config=nolint             # user override
  BBR_REMOTE_ARGS="--timeout=600" bbr test    # ad-hoc `bb remote` flag
"""


def main() -> None:
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(_HELP)
        return

    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")

    repo = pygit2.Repository(".")
    if warning := check_base_branch_freshness(repo):
        print(warning, file=sys.stderr)
    cmd = build_command(repo, args)

    if dry_run:
        print(" ".join(cmd))
        return

    result = subprocess.run(cmd, check=False)
    _print_post_run_summary()
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
