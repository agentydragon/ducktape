# Clean Up Agent Team

Remove team worktrees, branches, and data.

## Instructions

1. Get team ID from parameter

2. Check team exists at `~/.claude/teams/${TEAM_ID}`

3. Show team summary:
   - Creation time
   - Number of agents
   - Channel message count
   - Worktree status (clean/dirty)

4. Remove worktrees:
   ```bash
   cd ~/.claude/worktrees/${TEAM_ID}
   for worktree in *; do
     git worktree remove "$worktree" --force
   done
   git worktree prune
   ```

5. Remove branches:
   ```bash
   git branch -D team/${TEAM_ID}
   git branch -D agent/${TEAM_ID}/*
   ```

6. Remove scratch directories:
   ```bash
   rm -rf scratch/${TEAM_ID}
   ```

7. Remove team data (unless --keep-data):
   ```bash
   rm -rf ~/.claude/teams/${TEAM_ID}
   ```

## Usage

```
/cleanup-team clever-fox-20240319-1030
/cleanup-team clever-fox-20240319-1030 --keep-data
```
