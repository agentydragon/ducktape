# Worktree System Design Evolution

This document captures the vision for evolving `wt` from a single-repository worktree manager into a comprehensive multi-repository development environment. The design emphasizes simplicity in the immediate term while laying foundations for more sophisticated workflows.

## Current State (Completed Rationalization)

The configuration system has been fully rationalized into a clean, predictable system using `WT_DIR`-based configuration.

### Configuration System

Single configuration approach:

1. `WT_DIR` environment variable points to configuration directory
2. `$WT_DIR/config.yaml` contains explicit configuration with no defaults
3. All paths are explicit and validated upfront during Configuration.resolve()

Configuration and daemon state live under the configured `WT_DIR` location, providing clean separation between configuration storage and git repository location. This eliminates confusion around multiple config sources and provides flexible deployment options.

## Multi-Repository Vision

The long-term vision transforms `wt` into a global development environment manager. Instead of managing worktrees for one repository, it becomes a coordinator for all your development work across multiple repositories.

### Global Daemon Architecture

A single global daemon will manage all repositories and their worktrees from `~/.wt/` (overridable via `WT_DIR` for testing). This daemon maintains a registry of known repositories and provides fast status across your entire development workspace.

The global configuration at `~/.wt/config.toml` will define repository mappings, default behaviors, and global settings like GitHub refresh intervals. Per-repository state will be cached under `~/.wt/repos/{repo-name}/` but managed centrally.

### Intelligent Command Resolution

Commands will support both explicit repository specification and intelligent context-aware resolution:

- `wt branch-name` - operates on the default repository
- `wt --repo=other-project branch-name` - explicit repository selection
- `wt other-project/branch-name` - repository/worktree syntax
- When inside a worktree, commands operate on that repository by default

This creates a natural workflow where you can quickly jump between different projects while maintaining the speed and convenience of worktree-based development.

### Structured Development Directories

The system will support standardized repository layouts where each managed repository has a consistent structure:

```
<managed-code-dir>/
├── repo/
│   ├── .git/          # Shared repository (potentially bare)
│   ├── main/          # Main branch worktree
│   ├── feature-x/     # Feature worktrees
│   └── experiment-y/
├── other-project/
│   ├── .git/
│   └── default/
└── quick-prototype/
    ├── .git/
    └── main/
```

### Advanced Repository Operations

The multi-repo system will support repository-level operations that feel natural:

- `wt clone git@github.com:foo/bar` - clone and set up initial worktree structure
- `wt repos` - list all managed repositories with their active worktrees
- Cross-repository operations for comparing implementations or moving code between projects

## Core Development Principles

### One Branch = One Worktree

The fundamental principle remains: enforce strict mapping between branches and worktrees. This prevents the cognitive overhead of tracking which branch is checked out where and enables powerful file-system-level operations between branches.

### Filesystem as Interface

Operations like "move file from branch A to branch B" become literal `mv` commands between worktrees. This transforms abstract git operations into concrete filesystem operations that are easier to reason about and automate.

## Implementation Phases

### ✅ Phase 1: Rationalization (Completed)

**Status: COMPLETE** - The current system has been fully rationalized with:

- WT_DIR-based configuration system with explicit, validated paths
- Frozen Configuration dataclass with upfront validation
- Eliminated error swallowing patterns throughout codebase
- Proper server authority for path operations via new RPC methods
- Clean naming (WtDaemon, WtClient) and removed dead code
- Modern test patterns with decorator-style patches

### 🔄 Phase 2: Complete Daemon Migration (In Progress)

Move remaining worktree create/delete operations to daemon while maintaining the global daemon foundation work. Path operations have been successfully moved to server authority.

### 📋 Phase 3: Global Foundation

Introduce global daemon architecture while maintaining backward compatibility. The global daemon will coordinate multiple single-repository instances, gradually centralizing state management and cross-repository operations.

### 🚀 Phase 4: Advanced Multi-Repository Features

Add sophisticated features like structured directory management, repository cloning operations, and cross-repository workflows. These features will build on the solid foundation established in earlier phases.

## Technical Implementation Details

### MCP Integration Strategy

All worktrees will inherit MCP server configurations from `~/.claude.json`, drawing from the master environment to avoid breaking during development. Future versions may support per-worktree MCP configurations or jailed Claude instances that operate within specific worktree contexts.

### Safety and Process Management

Enhanced `wt rm` will use `lsof` or similar tools to detect active processes in worktrees before deletion. Git status checks and branch merge verification will prevent accidental loss of work. The system will provide clear feedback about what operations are safe and what might be destructive.

### Performance Optimizations

The system will use COW (copy-on-write) techniques, particularly APFS clonefile on macOS, for faster worktree hydration. Daemon health monitoring will continuously attempt to start `gitstatusd` processes where needed, reporting availability through a health RPC that clients can check to provide appropriate user feedback.

### Integration Ecosystem

IDE integration will generate appropriate workspace files (VS Code, etc.) for each worktree. Terminal integration will support intelligent shell prompts and directory jumping. The system will integrate cleanly with existing git workflows while encouraging the worktree-based approach.

# Template Command System (Future Bikeshedding Paradise)

## The Beautiful Over-Engineering Temptation

```bash
# Define reusable command templates
wt template diff-configs "diff {a}/config.yaml {b}/config.yaml"
wt template copy-experiments "cp -r {a}/experiments/ {b}/"
wt template run-in "cd {a} && {cmd}"

# Use templates with worktree substitution
wt diff-configs main feature
wt copy-experiments old-experiment new-experiment
wt run-in feature "python test.py"

# Multi-worktree operations
wt template multi-diff "diff {a}/foo {b}/foo {c}/foo"
wt multi-diff main feature-1 feature-2
```

### Why This Is Sexy But Dangerous

- Reminds me of 4 different esoteric programming languages
- Definitely somewhere nontrivial on the language complexity scale
- Would be absolutely ridiculous rabbit hole for a productivity tool
- But SO beautiful and tempting for an ADHD brain
- High reward episode for LLM collaboration

### Practical Multi-Worktree Operations (Maybe Later)

```bash
# Multi-path operations
wt path main feature /config.yaml  # Returns both paths for diff
diff $(wt path main feature /config.yaml)

# Bulk operations
wt foreach "git status"  # Run command in all worktrees
wt map feature-* "python test.py"  # Run in matching worktrees
```

## Advanced Path Operations

### Path Resolution Ideas

```bash
# Current design we're implementing
wt path <x> /foo/bar          # <x root>/foo/bar
wt path <x> /foo/bar --relative  # Relative to pwd

# Future extensions
wt path --common main feature    # Common ancestor path
wt path --diff main feature     # Paths that differ between worktrees
wt glob feature-* /src/*.py     # Glob across multiple worktrees
```

## Workflow Shortcuts

### Copy and Branch Patterns

```bash
# What we're implementing
wt cp <x> <y>                   # Copy worktree x to new worktree y

# Future extensions
wt cp <x> <y> --clean           # Copy structure but clean git state
wt cp <x> <y> --link            # Hard link shared files
wt merge-back <x>               # Merge worktree back to master and cleanup
```

## Note to Future Self

Remember: The goal is building a productivity tool for ML research, not a programming language. Ship the practical version first, then maybe explore the beautiful abstractions when they solve real problems we actually encounter.
