# GitHub Copilot Coding Agent Configuration

## Overview

This document explains how GitHub Copilot coding agent works with this repository and what configuration is available.

## What is GitHub Copilot Coding Agent?

GitHub Copilot coding agent is an AI agent that you can assign issues to on GitHub.com. When assigned, it:

- Analyzes the issue and repository context
- Creates a plan and implements changes
- Opens a pull request with the changes
- Responds to feedback and comments

## How GitHub Copilot Coding Agent Works

1. **Runs on GitHub Infrastructure**: The agent executes in an ephemeral GitHub Actions environment
2. **Uses Repository Context**: It reads your code, issues, PRs, and configuration files
3. **Follows Custom Instructions**: It reads `.github/copilot-instructions.md` for repo-specific guidance
4. **Custom Environment Setup**: You can customize the environment via `.github/workflows/copilot-setup-steps.yml`

## Available Configuration

### 1. Environment Setup (`.github/workflows/copilot-setup-steps.yml`) ⭐ NEW

**What it is**: A GitHub Actions workflow that runs before Copilot starts working, allowing you to install dependencies, tools, and configure the environment.

**Location**: `.github/workflows/copilot-setup-steps.yml`

**This repository's setup**:

- ✅ Installs Python 3.13
- ✅ Installs Bazelisk (for building with Bazel)
- ✅ Caches Bazel artifacts for faster builds
- ✅ Installs pre-commit hooks
- ✅ Installs cluster tools (opentofu, tflint)

**What you can customize**:

- Install specific versions of languages (Python, Node.js, Go, etc.)
- Install databases (PostgreSQL, MySQL, Redis, etc.)
- Install system dependencies
- Set up services using Docker containers
- Cache dependencies for faster setup
- Set environment variables (via GitHub Actions secrets/variables in the `copilot` environment)

**Documentation**: [Customize the agent environment](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment)

### 2. Repository Custom Instructions (`.github/COPILOT_INSTRUCTIONS.md`)

**What it is**: Instructions file that tells Copilot how to work with your repository.

**Location**: `.github/COPILOT_INSTRUCTIONS.md` (this repository already has this file)

**What to include**:

- Repository overview and structure
- Build and test commands
- Code style guidelines
- Common pitfalls and workarounds
- Verification steps

**Example from this repository**:

```markdown
## Build System

bazel build //... # Build all targets
bazel test //... # Run all tests
bazel lint //... # Lint (ruff + mypy)
```

### 3. Agent Instructions (`AGENTS.md` files)

**What it is**: Per-directory instructions for AI agents.

**Location**: Can be anywhere in the repository. The nearest `AGENTS.md` in the directory tree takes precedence.

**This repository has**: `AGENTS.md` in the root directory

### 4. Path-Specific Instructions

**What it is**: Instructions that apply to specific file paths.

**Location**: `.github/instructions/NAME.instructions.md`

**Not currently used in this repository**.

## Comparison with Claude Code Hooks

| Feature                      | Claude Code Hooks                      | GitHub Copilot Coding Agent            |
| ---------------------------- | -------------------------------------- | -------------------------------------- |
| **Execution Environment**    | User's local machine or gVisor sandbox | GitHub's managed infrastructure        |
| **Environment Setup**        | Custom via `session_start.py`          | Managed by GitHub (no custom setup)    |
| **Proxy Configuration**      | Supported (required for Claude Web)    | Not needed (GitHub handles networking) |
| **Custom Tool Installation** | Supported (bazelisk, nix, etc.)        | Not supported (uses GitHub's tooling)  |
| **Configuration Method**     | Python hooks + shell scripts           | Markdown instructions files            |
| **Working Directory**        | User's repository clone                | Temporary GitHub workspace             |

## What GitHub Copilot Coding Agent Has Access To

✅ **Available**:

- All repository code and files
- Git history
- Issues and PRs
- GitHub Actions workflows
- Custom instructions from `.github/copilot-instructions.md`
- Standard development tools (node, python, go, etc.)
- GitHub's build infrastructure

❌ **Not Available**:

- Custom environment setup scripts
- Local proxy configuration
- Custom CA certificates
- Supervisor/process management
- Local service configuration (podman, docker daemon, etc.)

## Best Practices for This Repository

### 1. Keep copilot-setup-steps.yml Updated

When you add new dependencies or tools to the project, update `.github/workflows/copilot-setup-steps.yml` to ensure Copilot has access to them.

**Example**: If you start using PostgreSQL in your tests:

```yaml
- name: Set up PostgreSQL
  run: |
    sudo systemctl start postgresql
    sudo -u postgres createdb testdb
```

### 2. Keep COPILOT_INSTRUCTIONS.md Updated

When you make significant changes to:

- Build system (Bazel configurations)
- Test infrastructure
- Development workflows
- Repository structure

Update `.github/COPILOT_INSTRUCTIONS.md` to reflect these changes.

### 3. Document Common Issues

Add common pitfalls and workarounds to the instructions:

```markdown
## Known Issues

- **Issue**: Bazel tests fail with "No such file or directory"
- **Solution**: Run `bazel clean` first, then `bazel test //...`
```

### 4. Provide Clear Build Instructions

Always include the exact commands to run:

```markdown
# Correct (specific)

bazel build //...

# Avoid (vague)

"Build the project"
```

### 5. Use AGENTS.md for Directory-Specific Context

For complex subdirectories (like `ansible/`), create local `AGENTS.md` files with context specific to that area.

## When to Use GitHub Codespaces Instead

If you need custom environment setup (like what Claude Code hooks provide), use **GitHub Codespaces** with `.devcontainer/devcontainer.json`:

- Custom Docker images
- Environment variables
- Tool installations via lifecycle hooks
- Service configuration

**Note**: Codespaces is for interactive development, not for the GitHub Copilot coding agent which runs automatically in the background.

## References

- [GitHub Copilot Documentation](https://docs.github.com/en/copilot)
- [Adding Custom Instructions](https://docs.github.com/en/copilot/customizing-copilot/adding-custom-instructions-for-github-copilot)
- [GitHub Copilot Coding Agent](https://github.com/features/copilot/agents)
- [Existing COPILOT_INSTRUCTIONS.md](../COPILOT_INSTRUCTIONS.md)
- [Existing AGENTS.md](../../AGENTS.md)

## Summary

**Environment Setup**: Use `.github/workflows/copilot-setup-steps.yml` to install tools, dependencies, and configure services before Copilot starts working.

**Instructions**: Use `.github/COPILOT_INSTRUCTIONS.md` and `AGENTS.md` files to provide guidance on how to work with the code.

**For Interactive Development**: Use GitHub Codespaces with `.devcontainer/` configuration if you need a persistent development environment.
