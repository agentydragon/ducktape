# Worktree ideas

The [wt README](../README.md) documents the current single-repository daemon, configuration,
path resolution, and worktree commands. The ideas below have no implementation schedule.
[Splitting a change into another worktree](SPLIT_FEATURE_DESIGN.md) has a separate proposal.

## Multi-repository daemon

A global daemon could maintain a registry of repositories, aggregate status, and let users switch
between projects with the same speed and convenience as switching between worktrees. The sketch
uses a shared configuration and per-repository state under `~/.wt/`, with `WT_DIR` still useful
for isolating test state. A transition would need to preserve existing single-repository
configurations while the global daemon coordinates them.

Select a repository from the current worktree, an explicit selector such as `--repo`, or a
`repo/branch` shorthand. Outside managed worktrees an unqualified branch could use a default
repository. Resolve precedence and ambiguous branch names before choosing the interface.
Possible operations include listing registered repositories and their worktrees, cloning a
repository into a standard layout, and comparing or moving files across repositories. One
suggested layout groups worktree directories with their shared Git repository, which could be
bare. Keep a predictable location for each checked-out branch so ordinary filesystem tools can
operate on worktrees.

## Workflow ideas

- **Paths and bulk commands.** Consider resolving several worktree paths for use with `diff`,
  finding their common ancestor or differing paths, or running commands on glob-selected
  worktrees. Try these concrete workflows before introducing a reusable command template
  language with worktree placeholders.
- **Copy and cleanup.** Possible shortcuts include copying only committed state and merging a
  worktree back before cleanup. Preserve the user's work when deciding which process, Git
  status, and merge checks a new cleanup operation needs. Hard-link copying would also need
  semantics for edits shared across worktrees.
- **Process health.** Review whether to restart a failed `gitstatusd` process and surface
  degraded readiness to clients.
- **Editor and agent integration.** Explore generating editor workspaces and preserving access
  to the user's existing MCP configuration. Per-worktree MCP configuration and isolated
  sessions are separate design questions.
