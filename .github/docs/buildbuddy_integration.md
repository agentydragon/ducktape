# BuildBuddy Integration

BuildBuddy credential setup is separated from repo-aware RBE opt-in:

- `devinfra/setup_buildbuddy.sh` writes `~/.config/bazel/buildbuddy.bazelrc`
  with the API key and `build --shell_executable=/bin/bash`.
- Repo-aware entrypoints decide whether to add `build --config=rbe`.

## Setup Chain

**GitHub Copilot** (`.github/workflows/copilot-setup-steps.yml`):

- `setup-bazel` action → `devinfra/setup_buildbuddy.sh`
- `setup-bazel` appends `build --config=rbe` to the ephemeral CI `~/.bazelrc`
- API key from `${{ secrets.BUILDBUDDY_API_KEY }}`

**Claude Code Hooks** (`devinfra/claude/claude_hook/main.rs` and Python hook daemon):

- Session start writes a per-session `buildbuddy.bazelrc`
- The per-session file includes the API key, `build --shell_executable=/bin/bash`,
  and `build --config=rbe`
- API key from `BUILDBUDDY_API_KEY` environment variable

## Key Differences

| Feature              | GitHub Copilot | Claude Code Hooks |
| -------------------- | -------------- | ----------------- |
| Network              | Direct         | Auth proxy        |
| Props Docker Network | props-agents   | host              |

Both gracefully degrade when API key is unavailable.
