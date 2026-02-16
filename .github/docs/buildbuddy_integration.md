# BuildBuddy Integration

Both GitHub Copilot and Claude Code hooks use `tools/setup_buildbuddy.sh` to configure BuildBuddy remote cache and RBE.

## Setup Chain

**GitHub Copilot** (`.github/workflows/copilot-setup-steps.yml`):

- `setup-bazel` action → `setup-buildbuddy` action → `tools/setup_buildbuddy.sh`
- API key from `${{ secrets.BUILDBUDDY_API_KEY }}`
- Toolchain detection enabled in `bazel-repo-cache` action

**Claude Code Hooks** (`tools/claude_hooks/session_start.py`):

- `buildbuddy_setup.setup_buildbuddy()` → `tools/setup_buildbuddy.sh`
- API key from `BUILDBUDDY_API_KEY` environment variable

## Key Differences

| Feature              | GitHub Copilot | Claude Code Hooks |
| -------------------- | -------------- | ----------------- |
| Network              | Direct         | Auth proxy        |
| Props Docker Network | props-agents   | host              |

Both gracefully degrade when API key is unavailable.
