# Main repository branch aliases
MAIN_REPO_ALIASES = {"main", "master"}

# Command descriptions (single source of truth for CLI commands and help text)
COMMAND_DESCRIPTIONS = {
    "ls": "List all worktrees",
    "status": "Show detailed status",
    "cp": "Copy worktree (with dirty state)",
    "rm": "Remove worktree (with safety checks)",
    "path": "Resolve worktree paths",
    "create": "Create new worktree (optionally from branch/worktree)",
    "help": "Show this help",
    "kill-daemon": "Kill the wt daemon",
}

# Command names derived from the descriptions mapping (single source of truth)
COMMAND_NAMES = set(COMMAND_DESCRIPTIONS.keys())

# All reserved names that cannot be used for worktree names
RESERVED_NAMES = MAIN_REPO_ALIASES | COMMAND_NAMES

# Display constants
MAIN_WORKTREE_DISPLAY_NAME = "main"
