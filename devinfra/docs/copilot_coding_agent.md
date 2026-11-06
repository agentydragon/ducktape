# GitHub Copilot coding agent setup

The repository setup owner is [copilot-setup-steps.yml](../../.github/workflows/copilot-setup-steps.yml).
Keep its job named `copilot-setup-steps` so Copilot can discover it.

The workflow installs Nix-defined devtools through
[setup-nix-devtools](../../.github/actions/setup-nix-devtools/action.yml), configures
BuildBuddy and the GitHub repository cache through
[setup-bazel](../../.github/actions/setup-bazel/action.yml), then installs pre-commit
hooks and checks tool availability. Shared tool definitions belong in
[nix/flake/devtools.nix](../../nix/flake/devtools.nix); follow those owners rather
than duplicating package versions or installer recipes here.

The workflow passes the repository's `BUILDBUDDY_API_KEY` secret to the Bazel setup
action. Review that workflow and action when changing credential wiring.

Repository guidance starts at the actual
[.github/copilot-instructions.md](../../.github/copilot-instructions.md), which
points to [AGENTS.md](../../AGENTS.md) and [STYLE.md](../../STYLE.md).

For upstream environment configuration, see
[Customize the agent environment](https://docs.github.com/en/copilot/how-tos/use-copilot-agents/coding-agent/customize-the-agent-environment).
