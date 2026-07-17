# Agent workspace image: interactive disposable dev workspaces, shared by all
# LLM-lane templates (../workspaces/app/). Deliberately its own
# image, not haku-zone-worker — workspaces are operator-facing and evolve
# independently of the haku dispatch plane. Built by
# .github/workflows/agent-workspace-image.yml (Dockerfile, not Bazel oci_image:
# the npm CLI installs need a package manager at build time).
#
# The image holds NO credentials: the LiteLLM virtual key arrives via the
# SandboxTemplate's env (reflected Secret).
FROM node:22.14.0-bookworm-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
    git ca-certificates python3 curl jq ripgrep less procps openssh-client tmux \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@2.1.198 @openai/codex@0.144.1 \
    && npm cache clean --force

# node:*-slim already owns UID 1000 (user `node`); rename it rather than
# useradd a second UID-1000 user (fails: "UID 1000 is not unique").
RUN usermod --login workspace --home /home/workspace --move-home node \
    && groupmod --new-name workspace node \
    && mkdir -p /workspace \
    && chown workspace:workspace /workspace

# z-claude parity (nix/home/claude_code/z-claude.nix): WebFetch/WebSearch are
# denied because GLM's tool-call shape differs from Anthropic's (see
# cluster/k8s/litellm/app/generate_litellm.py) — the calls fail confusingly
# through the GLM lane. Model pinning comes from the SandboxTemplate env.
COPY --chown=workspace:workspace claude-settings.json /home/workspace/.claude/settings.json

# Skip Claude Code first-run friction, same knobs as the mkClaudeGateway
# wrappers (nix/home/claude_code/gateway.nix): IS_DEMO skips the theme/
# onboarding flow, DISABLE_AUTOUPDATER pins the baked CLI version (updates
# come through image rebuilds, not in-pod self-updates). The seeded
# ~/.claude.json additionally pre-trusts /workspace so the trust dialog
# never fires there (codex-pod does the same for codex's directory trust).
ENV IS_DEMO=1 \
    DISABLE_AUTOUPDATER=1
COPY --chown=workspace:workspace claude.json /home/workspace/.claude.json

# Codex → LiteLLM provider config (codex lane; inert elsewhere — the env_key
# it names is only set by the codex-lane SandboxTemplate).
COPY --chown=workspace:workspace codex-config.toml /home/workspace/.codex/config.toml

USER workspace
WORKDIR /workspace

LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

CMD ["sleep", "infinity"]
