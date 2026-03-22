# `wt` - Git Worktree Management with COW

Quick switching between git worktrees with copy-on-write for rapid prototyping.

## Features

- Quick switching with relative path preservation
- Copy-on-write worktree duplication (macOS: clonefile, Linux: reflink, fallback: rsync)
- Process detection for safe cleanup
- Zsh integration via fd3 IPC
- GitHub PR status via background daemon

## Requirements

Python 3.13+, `gitstatusd` (Bazel provides via `//third_party/gitstatusd` for tests; for non-Bazel, install separately or set `gitstatusd_path` in config).

## Usage

```bash
wt feature-branch       # Switch to worktree
wt -c new-feature       # Create new worktree
wt ls                   # List worktrees
wt rm old-feature       # Remove (with safety checks)
wt cp experiment-v2     # Copy current worktree (COW)
wt path feature /src    # Get path in worktree
```

Install shell function: `eval "$(python -m wt.shell.install)"`

## Architecture

**Client-server**: CLI parses args, delegates to background daemon via JSON-RPC over Unix socket.

- **CLI** (`cli.py`): arg parsing, never calls GitHub APIs
- **Daemon**: GitHub API, git status queries, auto-starts when needed
- **Handlers** (`handlers.py`): pure functions with explicit deps

**Shell integration** uses fd3: CLI writes shell commands to fd3, shell function executes them after CLI exits. Exit codes: 0=success (execute), 1=uncontrolled error (skip), 2=controlled error (execute safe recovery).

**Path preservation**: detects relative position, maintains same path in target worktree, walks up directory tree if path doesn't exist.

## Configuration

`$WT_DIR/config.yaml` (set `WT_DIR` env var):

```yaml
main_repo: /path/to/repo # Required
worktrees_dir: /path/to/worktrees # Required
branch_prefix: feature/ # Required
upstream_branch: main # Required
github_repo: owner/repo # Required when github_enabled=true
```

Optional: `log_operations`, `cow_method` (auto|reflink|copy|rsync), `hydrate_worktrees`, `github_enabled`, `gitstatusd_path`, `post_creation_script`, `post_creation_timeout`.

### Post-creation hook FD behavior

- stdin: `/dev/null` (avoids CPython init crashes)
- stdout/stderr: captured pipes, streamed to client as `hook_output` events
- fd3: not used (fd3 is CLI-to-shell only)
- Daemon stdout goes to `$WT_DIR/daemon.log`
