# Architecture Documentation

## Overview

`wt` follows a **clean client-server architecture** with explicit dependency injection and clear separation between client operations and daemon-handled services.

## Directory Structure

```
wt/
├── cli.py                   # Main CLI entry point
├── client/                  # Client-side code (no GitHub APIs)
│   ├── wt_client.py         # Unix socket communication (WtClient)
│   ├── handlers.py          # Pure handler functions
│   ├── view_formatter.py    # Display formatting
│   ├── cd_utils.py          # Shell 'cd' command emission helper
│   └── shell_utils.py       # Shell command emission
├── server/                  # Server-side code (daemon only)
│   ├── wt_server.py         # Main daemon process (WtDaemon)
│   ├── git_manager.py       # Git operations for daemon
│   ├── github_client.py     # GitHub API client
│   ├── gitstatusd_client.py # GitStatusd communication
│   ├── worktree_ids.py      # WorktreeID generation
│   └── worktree_service.py  # Business logic for daemon
└── shared/                  # Shared models and utilities
    ├── config_file.py       # Configuration file schema
    ├── configuration.py     # Resolved configuration
    ├── protocol.py          # JSON-RPC protocol definitions
    ├── constants.py         # Shared constants
    ├── error_handling.py    # Error utilities
    ├── github_models.py     # GitHub data models
    └── models.py            # Core data structures
```

## Key Architectural Principles

### **Client Never Calls GitHub APIs**

- **Strict boundary**: Client-side code cannot import GitHub interfaces
- **All GitHub operations** delegated to daemon via JSON-RPC
- **Clean separation** between local operations and remote API calls
- **Server authority**: All path manipulation logic moved to server

### **Pure Handler Functions**

- Each handler declares exactly what it needs
- Easy to test and reason about

```python
# Pure function signatures
async def handle_status(daemon_client, formatter) -> None:
async def handle_status_single(daemon_client, formatter, worktree_name: str) -> None:
def handle_create_worktree(config, name: str, from_master: bool = True) -> None:
```

## Protocol Example

```python
# Client sends status request
{
    "method": "get_status",
    "id": "uuid-123",
    "params": {"force_refresh": false}
}

# Daemon responds with WorktreeGitStatus results
{
    "id": "uuid-123",
    "result": {
        "results": {
            "wtid:main": {"name": "main", "branch_name": "master", "ahead_count": 0, ...},
            "wtid:feature": {"name": "feature", "branch_name": "test/feature", "ahead_count": 2, ...}
        },
        "total_processing_time_ms": 150.5,
        "daemon_health": {"status": "ok"}
    }
}

# New path resolution methods
{
    "method": "worktree_resolve_path",
    "params": {
        "worktree_name": "feature",
        "path_spec": "/src/main.py",
        "current_path": "/current/working/dir"
    }
}

{
    "method": "worktree_teleport_target",
    "params": {
        "target_name": "feature",
        "current_path": "/current/working/dir"
    }
}
```

## Data Flow

### 1. **Status Operations** (Daemon-handled)

```
CLI → handle_status() → daemon_client.get_all_worktree_status()
                            ↓
                      Unix Socket Request
                            ↓
                        Daemon Process
                            ↓
                    Git Commands + GitHub API
                            ↓
                        JSON Response
                            ↓
                    WorktreeStatus objects
                            ↓
                    ViewFormatter.render()
                            ↓
                        Console Output
```

### 2. **Worktree Operations** (Daemon authority)

```
# Path Operations (Server-side)
CLI → daemon_client.resolve_path() → Unix Socket → Daemon → Path Resolution

# Create/Delete Operations (Server-side via RPC)
CLI → handlers → WtClient → JSON-RPC → WtDaemon → WorktreeService / GitManager

# Navigation emission (Client-side only)
CLI receives resolved path from daemon and emits `cd` via client/shell_utils
```
