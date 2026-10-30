"""bbr: bb remote with session metadata and invocation tracking.

Leaves runner defaults to bb's native .bbrc and forwards session-level Bazel
flags from $BBR_BAZELRC.
See devinfra/buildbuddy/docs/remote.md for how bb remote works under the hood.
"""

import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import pygit2

# Commits HEAD can be ahead of the likely bb-remote diff base before we refuse
# to run (see check_base_branch_freshness) — at that distance the runner-side
# patchset tends to fail to apply outright, after minutes of setup.
_STALE_BASE_ERROR_THRESHOLD = 30

# Bazel commands recognized by bb remote (from BuildBuddy cli/parser/bazelrc).
_BAZEL_COMMANDS = {
    # keep-sorted start
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
    # keep-sorted end
}


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


def check_remote_fetch_url(repo: pygit2.Repository) -> str | None:
    """Reject local clone sources that a hosted runner cannot fetch."""
    remote = _config_get(repo, "buildbuddy.remote-bazel-remote-name") or "origin"
    if _config_get(repo, f"remote.{remote}.url") is None:
        return None
    try:
        # Match bb's effective URL, including Git's insteadOf rewrites.
        url = subprocess.check_output(
            ["git", "remote", "get-url", remote], cwd=repo.workdir, stderr=subprocess.PIPE, text=True
        ).strip()
    except subprocess.CalledProcessError:
        return None  # Let bb explain an invalid/missing remote.
    parsed = urlsplit(url)
    # Git also accepts [user@]host:path (including bracketed IPv6 hosts).
    # A slash before the colon makes it a local path instead. See git-clone(1).
    scp_remote = re.fullmatch(r"(?:[^/@:\s]+@)?(?:\[[^\]]+\]|[^/:\s]+):.+", url)
    if parsed.scheme == "file" or (not (parsed.scheme and parsed.netloc) and not scp_remote):
        return (
            f"bbr: selected remote {remote!r} resolves to a local filesystem path, "
            "which the hosted runner cannot fetch. Configure an externally reachable "
            "Git remote and select it with git config buildbuddy.remote-bazel-remote-name <name>."
        )
    return None


def check_base_branch_freshness(repo: pygit2.Repository) -> str | None:
    """Best-effort, no-network check on bb remote's likely diff base.

    `bb remote` mirrors local git state to the runner as a base commit +
    patchset (see devinfra/buildbuddy/docs/remote.md). When the current
    branch has no tracking ref on the BuildBuddy remote, it falls back to
    `<default-branch>@{upstream}` as the base — if that local tracking ref
    is stale (no recent `git fetch`), the patchset balloons and can even fail
    outright (unappliable binary patches on a huge diff). This deliberately
    never fetches — bbr running network calls on every invocation would be
    its own surprise — it only inspects locally-known refs, so a message here
    means "you should `git fetch`", not "bbr already tried and failed".

    The default branch name comes only from `buildbuddy.remote-bazel-default-branch`,
    which `bb remote` itself detects and caches on every run — never a
    hardcoded guess. Returns an error message (main() refuses to run unless
    BBR_ALLOW_STALE_BASE is set — past the threshold the run is near-certain
    to waste minutes and fail), or None if there's nothing to flag (including
    "couldn't tell" — silence, not a guess).
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
    if ahead <= _STALE_BASE_ERROR_THRESHOLD:
        return None

    return (
        f"bbr: HEAD is {ahead} commits ahead of {remote}/{default_branch}, "
        f"bb remote's likely diff base for this branch — the patchset it generates "
        f"would be huge and likely fail to apply on the runner. Fix: "
        f"git fetch {remote} {default_branch} (also fetch {current_branch} there "
        f"if it's pushed, so bb can base on it directly). "
        f"Set BBR_ALLOW_STALE_BASE=1 to run anyway."
    )


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


def _explicit_invocation_id(command_options: list[str]) -> str | None:
    """The caller's own --invocation_id from after the verb, if any (last wins, as in Bazel)."""
    found = None
    for i, arg in enumerate(command_options):
        if arg.startswith("--invocation_id="):
            found = arg.removeprefix("--invocation_id=")
        elif arg == "--invocation_id" and i + 1 < len(command_options):
            found = command_options[i + 1]
    return found


def build_command(user_args: list[str]) -> tuple[list[str], str | None]:
    """Assemble the full bb remote command line; returns it with this run's invocation ID.

    Argument layout:
      bb remote [startup/remote flags] <verb> [--invocation_id] [session bazelrc] [user flags+targets]

    bbr mints the invocation ID and passes it into the remote Bazel command, so
    the ID it reports is this run's by construction. Reading it back from bb's
    --invocation_id_file is unsound: concurrent bbr runs sharing that path
    cross-attribute each other's runs (bb_remote_internals.md § Invocation IDs).
    """
    bb = _find_bb()

    # Split user_args at the bazel verb into startup options (before verb)
    # and command options (after verb).
    verb_idx = find_verb_index(user_args)
    if verb_idx is not None:
        startup_options = user_args[:verb_idx]
        verb = user_args[verb_idx]
        command_options = user_args[verb_idx + 1 :]
        explicit_id = _explicit_invocation_id(command_options)
        invocation_id = explicit_id or str(uuid.uuid4())
        invocation_id_flag = [] if explicit_id else [f"--invocation_id={invocation_id}"]
    else:
        # No bazel verb (e.g. --script): no Bazel command
        # line to attach --invocation_id to, so no ID is minted or reported.
        startup_options = user_args
        verb = None
        command_options = []
        invocation_id = None
        invocation_id_flag = []

    cmd = [
        bb,
        "remote",
        *startup_options,
        *([verb] if verb else []),
        *invocation_id_flag,
        *_bazelrc_args(),
        *command_options,
    ]
    return cmd, invocation_id


def _bb_environment() -> dict[str, str] | None:
    """Pass a mounted key to bb only for this run, never to dry-run output or repo configuration.

    `bb remote` forwards this key to hosted Bazel. The caller must trust both the sandbox
    and the hosted runner with the key; a file mount is not an isolation boundary.
    """
    file = os.environ.get("BBR_BUILDBUDDY_API_KEY_FILE")
    if not file or os.environ.get("BUILDBUDDY_API_KEY"):
        return None
    key = Path(file).read_text().strip()
    if not key:
        raise ValueError("BBR_BUILDBUDDY_API_KEY_FILE is empty")
    return {**os.environ, "BUILDBUDDY_API_KEY": key}


def _extract_invocation_id_file(args: list[str]) -> tuple[list[str], Path | None]:
    """Strip bbr's own --invocation-id-file=PATH flag from args (last wins)."""
    path = None
    remaining = []
    for arg in args:
        if arg.startswith("--invocation-id-file="):
            path = Path(arg.removeprefix("--invocation-id-file="))
        else:
            remaining.append(arg)
    return remaining, path


_HELP = """\
bbr — bb remote with session metadata, invocation tracking, and a Git-base check.

Usage: bbr [--help] [--dry-run] [--invocation-id-file=PATH] [remote flags...] <bazel-verb> [flags...] [targets...]

Runner defaults come from bb's native .bbrc files; pass remote flags before
the Bazel verb to override them. Bazel defaults come from the
runner's .bazelrc. $BBR_BAZELRC supplies session metadata before user arguments.

Flags:
  --dry-run                  Print the forwarded command without expanding .bbrc
  --invocation-id-file=PATH  Write the run's invocation ID to PATH before the run
  --help                     Show this help

Environment variables:
  BBR_BAZELRC       Path to a bazelrc-format file with Bazel flags to forward.
                    Lines are parsed as "<command> <flag>" (prefix stripped).
  BBR_BUILDBUDDY_API_KEY_FILE  Optional mounted key file, passed to bb for real runs only.
  BBR_ALLOW_STALE_BASE  Run even when the likely diff base looks stale (bbr
                    normally refuses — see bb_remote_internals.md).

Examples:
  bbr test //foo:bar                          # basic test
  bbr build //foo --config=nolint             # user override
  bbr --timeout=10m test //foo:bar             # override a bb remote option
"""


def main() -> None:
    args = sys.argv[1:]

    if "--help" in args or "-h" in args:
        print(_HELP)
        return

    dry_run = "--dry-run" in args
    if dry_run:
        args.remove("--dry-run")
    args, invocation_id_file = _extract_invocation_id_file(args)

    repo = pygit2.Repository(".")
    if message := check_remote_fetch_url(repo):
        print(message, file=sys.stderr)
        raise SystemExit(1)
    if message := check_base_branch_freshness(repo):
        print(message, file=sys.stderr)
        if not os.environ.get("BBR_ALLOW_STALE_BASE"):
            sys.exit(1)
    cmd, invocation_id = build_command(args)
    if invocation_id is None and invocation_id_file is not None:
        print("bbr: --invocation-id-file: no bazel verb, so no invocation ID to record", file=sys.stderr)
        sys.exit(1)

    if dry_run:
        print(" ".join(cmd))
        return

    if invocation_id is not None:
        # Printed and recorded before exec so interrupted runs still leave the ID.
        print(f"bbr: invocation {invocation_id}", file=sys.stderr)
        if invocation_id_file is not None:
            invocation_id_file.parent.mkdir(parents=True, exist_ok=True)
            invocation_id_file.write_text(invocation_id)

    # Do not pass an env override unless a key file was explicitly configured.
    env = _bb_environment()
    result = subprocess.run(cmd, check=False) if env is None else subprocess.run(cmd, check=False, env=env)
    if invocation_id is not None:
        print(
            f'bbr: invocation {invocation_id}  (bbapi {{target,"target log",artifact,invocation}} {invocation_id})',
            file=sys.stderr,
        )
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
