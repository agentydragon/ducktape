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
        "cal",
        "uptime",
        "cat",
        "head",
        "tail",
        "wc",
        "stat",
        "strings",
        "hexdump",
        "od",
        "nl",
        "id",
        "uname",
        "free",
        "df",
        "du",
        "locale",
        "groups",
        "nproc",
        "basename",
        "dirname",
        "realpath",
        "cut",
        "paste",
        "tr",
        "column",
        "tac",
        "rev",
        "fold",
        "expand",
        "unexpand",
        "fmt",
        "comm",
        "cmp",
        "numfmt",
        "readlink",
        "diff",
        "true",
        "false",
        "sleep",
        "which",
        "type",
        "expr",
        "test",
        "getconf",
        "seq",
        "tsort",
        "pr",
        "echo",
        "printf",
        "ls",
        "cd",
        "find",
    ]
)

# Commands auto-allowed with safe-flags only (validated per-flag by Claude Code).
# The scanner treats these as auto-allowed since the command name is recognized.
_FLAG_VALIDATED_CMDS = frozenset(
    [
        "xargs",
        "file",
        "sed",
        "sort",
        "man",
        "help",
        "netstat",
        "ps",
        "base64",
        "grep",
        "egrep",
        "fgrep",
        "sha256sum",
        "sha1sum",
        "md5sum",
        "tree",
        "date",
        "hostname",
        "info",
        "lsof",
        "pgrep",
        "tput",
        "ss",
        "fd",
        "fdfind",
        "pyright",
        "rg",
        "jq",
        "uniq",
        "history",
        "arch",
        "ifconfig",
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
        "status",
        "log",
        "diff",
        "show",
        "blame",
        "branch",
        "tag",
        "remote",
        "ls-files",
        "ls-remote",
        "config",
        "rev-parse",
        "rev-list",
        "describe",
        "reflog",
        "shortlog",
        "cat-file",
        "for-each-ref",
        "worktree",
        "name-rev",
        "merge-base",
        "grep",
    ]
)

_GH_READONLY = frozenset(
    [
        "pr view",
        "pr list",
        "pr diff",
        "pr checks",
        "pr status",
        "issue view",
        "issue list",
        "issue status",
        "run list",
        "run view",
        "workflow list",
        "workflow view",
        "repo view",
        "release list",
        "release view",
        "auth status",
        "label list",
        "search repos",
        "search issues",
        "search prs",
        "search commits",
        "search code",
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
