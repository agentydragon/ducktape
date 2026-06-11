# BuildBuddy Integration

Both GitHub Copilot and Claude Code hooks use `devinfra/setup_buildbuddy.sh` to configure BuildBuddy remote cache and RBE.

## Setup Chain

**GitHub Copilot** (`.github/workflows/copilot-setup-steps.yml`):

- `setup-bazel` action → `devinfra/setup_buildbuddy.sh`
- API key from `${{ secrets.BUILDBUDDY_API_KEY }}`

**Claude Code Hooks** (`devinfra/claude/session_start.py`):

- `buildbuddy_setup.setup_buildbuddy()` → `devinfra/setup_buildbuddy.sh`
- API key from `BUILDBUDDY_API_KEY` environment variable

## Key Differences

| Feature              | GitHub Copilot | Claude Code Hooks |
| -------------------- | -------------- | ----------------- |
| Network              | Direct         | Auth proxy        |
| Props Docker Network | props-agents   | host              |

Both gracefully degrade when API key is unavailable.
