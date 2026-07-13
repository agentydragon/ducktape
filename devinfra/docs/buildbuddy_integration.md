# BuildBuddy Integration

The rc ownership and RBE execution contract live in
<bazel_configuration.md>. This page records only environment-specific setup.

## Setup Chain

**GitHub Copilot** (`.github/workflows/copilot-setup-steps.yml`):

- `setup-bazel` action → `devinfra/setup_buildbuddy.sh`
- Ducktape's workspace `.bazelrc` selects RBE
- API key from `${{ secrets.BUILDBUDDY_API_KEY }}`

**Claude Code Hooks** (`devinfra/claude/claude_hook/main.rs` and Python hook daemon):

- Session start writes a per-session `buildbuddy.bazelrc`
- The per-session file contributes only a credential scoped to `rbe`
- API key from `BUILDBUDDY_API_KEY` environment variable

## Key Differences

| Feature              | GitHub Copilot | Claude Code Hooks |
| -------------------- | -------------- | ----------------- |
| Network              | Direct         | Auth proxy        |
| Props Docker Network | props-agents   | host              |

Both gracefully degrade when API key is unavailable.
