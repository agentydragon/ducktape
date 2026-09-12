# Claude Security Model - Directory Access Findings

## Key Discovery: Task Tool Limitations

When spawning agents via Claude's Task tool, there is **no way to pass command-line flags** like `--add-dir` to grant additional directory access.

### What We Tested

1. **Current session access**: The current Claude session CAN cd to parent directories and outside the original working directory
2. **--add-dir flag**: The `claude` CLI supports `--add-dir` to grant access to additional directories
3. **Task tool spawning**: Agents spawned via Task tool get their own security context with no way to pass CLI flags

### Implications for Spawn-Graph

The original plan to use centralized worktree storage in `~/.claude/projects/{repo-name}/spawn-graph/` **won't work** because:

- Spawned agents start with restricted directory access
- The Task tool API doesn't support passing `--add-dir` or other CLI flags
- Agents would be unable to cd into their assigned worktrees

### Conclusion

**Spawn-graph worktrees MUST be created within the repository directory tree**, not in a centralized location.
This creates some clutter but is the only way spawned agents can access their worktrees.

### Future Considerations

If the Task tool API is ever enhanced to support passing CLI flags, we could revisit centralized worktree storage.
For now, local `./spawn-graph/` directories are required.
