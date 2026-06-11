# `wt` - Git Worktree Management with COW

Makes switching between git worktrees feel like `git switch` while adding copy-on-write functionality for rapid prototyping.

## Features

- **Quick switching** between worktrees with relative path preservation
- **Copy-on-write operations** for duplicating worktrees with uncommitted changes
- **Path resolution** with absolute (`/foo`) and relative (`./foo`) path support
- **Process detection** for safe worktree cleanup
- **Operation logging**
- **Zsh integration** for seamless shell navigation

## Requirements

- **Python 3.13+**
- **gitstatusd** (for fast git status queries): installed and available on `PATH`

Tests will fail immediately if gitstatusd is missing.

## Development

See the repository root AGENTS.md for the standard Bazel workflow.

```bash
bazel run //wt:wt-cli -- --help
bazel test //wt/...
bazel build --config=check //wt/...  # lint + typecheck
```

## Installation

- Add the wt shell function in your shell init (e.g. `.bashrc` / `.zshrc`): `eval "$(python -m wt.shell.install)"`
- Reload your shell / source the same dotfile.

## Usage

### Basic Commands

```bash
# Switch to existing worktree (or offer to create)
wt feature-branch

# Explicitly create new worktree from master
wt -c new-feature

# List all worktrees
wt ls

# Remove worktree (with safety checks)
wt rm old-feature

# Remove worktree forcefully
wt rm old-feature --force
```

### GitHub PR Status

All worktree status commands automatically include GitHub pull request information via the background daemon.

### Special Destinations

```bash
# Switch to main repo
wt main
wt master
```

### Copy Operations (COW)

```bash
# Copy current worktree to new one (preserves dirty state)
wt cp experiment-v2

# Copy specific worktree to new one
wt cp feature-a feature-b
```

### Path Operations

```bash
# Get current worktree root
wt path

# Get specific worktree root
wt path feature-branch

# Get absolute path from worktree root
wt path feature-branch /src/main.py

# Get path relative to current position
wt path feature-branch ./test.py

# Get path in current worktree
wt path /config.yaml
wt path ./relative/file.py
```

## Architecture

### Client-Server Design

`wt` uses a **daemon-first architecture** to separate concerns and improve performance:

#### **CLI Client** (`cli.py`)

- Pure argument parsing and coordination
- **Never calls GitHub APIs** - delegates to daemon
- Creates individual services (daemon_client, formatter, config)
- Passes explicit dependencies to handlers

#### **Background Daemon** (`daemon.py`)

- Handles **all GitHub API operations**
- Performs git repository status queries
- JSON-RPC server over Unix socket
- Proper daemonization with file logging
- Auto-starts when needed by client
- Renamed from GitStatusdDaemon → WtDaemon for clarity

#### **Handler Functions** (`handlers.py`)

- Pure functions with explicit dependencies
- Status operations → daemon client
- Worktree operations → direct git commands (see Future Plans)
- No service containers or hidden dependencies

### Data Flow

CLI → JSON-RPC over Unix Socket → Daemon

### Shell Integration

The `wt` function uses IPC via file descriptor 3:

1. **Pipe Creation**: Creates anonymous pipes for bidirectional communication
2. **Command Execution**: Python script writes shell commands to fd 3
3. **Exit Code Handling**:
   - `0`: Success - execute commands
   - `1`: Uncontrolled error - don't execute anything
   - `2`: Controlled error - execute commands (safe recovery)
4. **Atomic Execution**: Commands only run if the tool completed successfully

This design allows:

- **Interactive prompts** that work normally
- **Clean error handling** with proper exit codes
- **Safe navigation** away from problematic locations
- **Normal stdout/stderr** for user messages

### Copy-on-Write

Uses platform-optimized COW operations:

- **macOS**: `cp -c -R` (clonefile)
- **Linux**: `cp --reflink=auto`
- **Fallback**: `rsync`

This enables instant duplication of entire worktrees including uncommitted changes.

### Path Preservation

When switching between worktrees, the tool:

1. Detects your current relative position
2. Tries to maintain the same path in the target worktree
3. Walks up the directory tree until it finds an existing path
4. Emits the appropriate `cd` command

### Safety Features

- **Process detection**: Uses `psutil` to check for running processes in worktree
- **Git status checks**: Prevents accidental deletion of dirty worktrees
- **Reserved name protection**: Blocks creation of worktrees with command names
- **Operation logging**: Tracks all create/remove operations for audit

### Branch Naming

- All worktrees use configurable branch naming scheme (no defaults)
- Reserved names (`ls`, `rm`, `status`, etc.) are blocked
- Special cases (`main`, `master`) teleport to main repo

## Configuration

The tool uses `WT_DIR` environment variable to locate configuration:

```bash
export WT_DIR=/path/to/.wt
```

Configuration file at `$WT_DIR/config.yaml` specifies:

- `main_repo`: Path to main git repository (required)
- `worktrees_dir`: Directory for worktrees (required)
- `branch_prefix`: Prefix for worktree branches (required)
- `upstream_branch`: Default upstream branch (required)
- `github_repo`: GitHub repository identifier (required)

Optional settings:

- `log_operations`: Enable operation logging (default: false)
- `cow_method`: auto | reflink | copy | rsync (default: auto)
- `hydrate_worktrees`: If false, newly created or copied worktrees are left unpopulated (default: true)
- `github_enabled`: Enable GitHub integration (default: true)
- `gitstatusd_path`: Path to gitstatusd binary (optional)
- `post_creation_script`: Script to run after creating a worktree (optional)
- `post_creation_timeout`: Seconds to wait for post-creation script before killing it (default: 60)

FD behavior of post-creation hooks

- stdin (fd 0): /dev/null. The daemon launches the hook with stdin=DEVNULL to guarantee a valid descriptor even when the parent process had no stdin; this avoids CPython init_sys_streams crashes. Do not rely on reading from stdin in post-create scripts.
- stdout (fd 1): Captured pipe. Hook stdout is streamed to the client as hook_output events; on failures, the CLI echoes captured output to the user.
- stderr (fd 2): Captured pipe. Same streaming/echo behavior as stdout.
- fd 3: Not used for hooks. The fd3 channel is only for CLI→shell navigation commands; hooks are launched by the daemon and do not use or require fd3.
- Other fds: closed on exec. Only 0/1/2 are set as above.

Daemon stdio summary (for context)

- Daemon stdout is redirected to `$WT_DIR/daemon.log`; stderr is `/dev/null`. This does not affect hook I/O, which is independently piped and streamed.

Sample config.yaml:

```yaml
main_repo: /path/to/repo
worktrees_dir: /path/to/repo/worktrees
branch_prefix: feature/
upstream_branch: main

# Optional
github_enabled: false
github_repo: owner/repo
log_operations: true
cow_method: auto # macOS: clonefile; Linux: reflink; fallback: rsync
hydrate_worktrees: true # set false to leave new worktrees empty
```

cow_method behavior:

- auto: clonefile on macOS; reflink if supported; else rsync
- reflink: force reflink, error if unsupported
- copy: clonefile on macOS, else reflink if available, else rsync
- rsync: always use rsync

## Directory Structure

```
~/code/
├── repo/              # Main repository
│   └── .git/
├── worktrees/         # Worktree directory
│   ├── feature-a/
│   ├── experiment/
│   └── bugfix/
└── .wt/               # Configuration and daemon state
    ├── config.yaml
    ├── daemon.sock
    └── daemon.pid
```

## Logs and Data

- **Configuration**: `$WT_DIR/config.yaml`
- **Daemon state**: `$WT_DIR/daemon.sock`, `$WT_DIR/daemon.pid`
- **Logs**: Daemon logs to configured location

## Examples

### Rapid Prototyping Workflow

```bash
# Start working on a feature
wt feature-work
# ... make changes, experiment ...

# Branch off current state for different approach
wt cp feature-alt
# ... now you have two copies with same starting point ...

# Switch back and forth
wt feature-work
wt feature-alt

# Clean up when done
wt rm feature-alt
```

### File Operations Between Worktrees

```bash
# Compare configs
diff $(wt path main /config.yaml) $(wt path feature /config.yaml)

# Copy files between worktrees
cp $(wt path feature-a /experiment.py) $(wt path feature-b/)

# Edit file in specific worktree
vim $(wt path feature /src/main.py)
```

### Path Resolution Examples

From `~/code/worktrees/feature/src/components`:

```bash
wt path                  # ~/code/worktrees/feature
wt path /tests           # ~/code/worktrees/feature/tests
wt path ./test.py        # ~/code/worktrees/feature/src/components/test.py
wt path main ./test.py   # ~/code/repo/src/components/test.py
```

## Troubleshooting

### Shell Function Not Working

Ensure the function is installed in your shell init and reload:

```bash
# Check if function is defined
type wt

# Install into current shell session
eval "$(python -m wt.shell.install)"

# Add permanently to ~/.zshrc and reload
echo 'eval "$(python -m wt.shell.install)"' >> ~/.zshrc
source ~/.zshrc
```

### Process Detection Issues

If `wt rm` incorrectly detects processes, you can force removal:

```bash
wt rm worktree-name --force
```
