"""Claude Code built-in read-only command validation.

Commands that Claude Code auto-allows without any permission entry.
Source: readOnlyValidation.ts, readOnlyCommandValidation.ts in the Claude Code binary.

This is a simplified approximation — the real validation has per-flag allowlists
for commands like grep, sort, date, etc. For scanner purposes, we identify which
command names/subcommands are auto-allowed at all, since flag-level validation
is too complex to replicate exactly and false positives (marking something covered
when it might not be) are the less harmful direction.
"""


def _skip_env_prefix(parts: list[str]) -> int:
    i = 0
    while i < len(parts) and "=" in parts[i] and not parts[i].startswith("-"):
        i += 1
    return i


# Any-args auto-allowed (simplified — real validator has regex constraints)
_BUILTIN_CMDS = frozenset(
    [
        # keep-sorted start
        "basename",
        "cal",
        "cat",
        "cd",
        "cmp",
        "column",
        "comm",
        "cut",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "expand",
        "expr",
        "false",
        "find",
        "fmt",
        "fold",
        "free",
        "getconf",
        "groups",
        "head",
        "hexdump",
        "id",
        "locale",
        "ls",
        "nl",
        "nproc",
        "numfmt",
        "od",
        "paste",
        "pr",
        "printf",
        "readlink",
        "realpath",
        "rev",
        "seq",
        "sleep",
        "stat",
        "strings",
        "tac",
        "tail",
        "test",
        "tr",
        "true",
        "tsort",
        "type",
        "uname",
        "unexpand",
        "uptime",
        "wc",
        "which",
        # keep-sorted end
    ]
)

# Commands auto-allowed with safe-flags only (validated per-flag by Claude Code).
# The scanner treats these as auto-allowed since the command name is recognized.
_FLAG_VALIDATED_CMDS = frozenset(
    [
        # keep-sorted start
        "arch",
        "base64",
        "date",
        "egrep",
        "fd",
        "fdfind",
        "fgrep",
        "file",
        "grep",
        "help",
        "history",
        "hostname",
        "ifconfig",
        "info",
        "jq",
        "lsof",
        "man",
        "md5sum",
        "netstat",
        "pgrep",
        "ps",
        "pyright",
        "rg",
        "sed",
        "sha1sum",
        "sha256sum",
        "sort",
        "ss",
        "tput",
        "tree",
        "uniq",
        "xargs",
        # keep-sorted end
    ]
)

# Commands auto-allowed exactly (no args or very specific arg patterns)
_EXACT_CMDS = frozenset(["pwd", "whoami", "alias"])

# Exact forms: command + specific args
_EXACT_FORMS = frozenset(
    ["claude -h", "claude --help", "node -v", "node --version", "python --version", "python3 --version", "ip addr"]
)

_GIT_READONLY = frozenset(
    [
        # keep-sorted start
        "blame",
        "branch",
        "cat-file",
        "config",
        "describe",
        "diff",
        "for-each-ref",
        "grep",
        "log",
        "ls-files",
        "ls-remote",
        "merge-base",
        "name-rev",
        "reflog",
        "remote",
        "rev-list",
        "rev-parse",
        "shortlog",
        "show",
        "status",
        "tag",
        "worktree",
        # keep-sorted end
    ]
)

_GH_READONLY = frozenset(
    [
        # keep-sorted start
        "auth status",
        "issue list",
        "issue status",
        "issue view",
        "label list",
        "pr checks",
        "pr diff",
        "pr list",
        "pr status",
        "pr view",
        "release list",
        "release view",
        "repo view",
        "run list",
        "run view",
        "search code",
        "search commits",
        "search issues",
        "search prs",
        "search repos",
        "workflow list",
        "workflow view",
        # keep-sorted end
    ]
)

_DOCKER_READONLY = frozenset(["ps", "images", "logs", "inspect"])

_KUBECTL_READONLY = frozenset(
    ["get", "describe", "logs", "top", "api-resources", "api-versions", "version", "cluster-info"]
)


def is_builtin_allowed(cmd: str) -> bool:
    """Check if Claude Code auto-allows this command without any config."""
    parts = cmd.split()
    if not parts:
        return True
    i = _skip_env_prefix(parts)
    if i >= len(parts):
        return True
    first = parts[i]
    if first == "sudo":
        i += 1
        if i >= len(parts):
            return True
        first = parts[i]

    if first in _BUILTIN_CMDS or first in _FLAG_VALIDATED_CMDS:
        return True

    # Exact forms: check full command string (after stripping env prefix)
    bare = " ".join(parts[i:])
    if bare in _EXACT_CMDS or bare in _EXACT_FORMS:
        return True

    if first == "git" and len(parts) > i + 1:
        sub = parts[i + 1]
        if sub in _GIT_READONLY:
            return True
        if sub == "stash" and len(parts) > i + 2 and parts[i + 2] in ("list", "show"):
            return True

    if first == "gh" and len(parts) > i + 1:
        sub = f"{parts[i + 1]} {parts[i + 2]}" if len(parts) > i + 2 else parts[i + 1]
        if sub in _GH_READONLY or parts[i + 1] in ("api", "search"):
            return True

    if first == "docker" and len(parts) > i + 1 and parts[i + 1] in _DOCKER_READONLY:
        return True

    return first == "kubectl" and len(parts) > i + 1 and parts[i + 1] in _KUBECTL_READONLY
