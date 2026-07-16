# Agent workspace image: interactive disposable dev workspaces
# (../workspaces/app/sandboxtemplate-workspace.yaml). Deliberately its own
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
    git ca-certificates python3 curl jq ripgrep less procps openssh-client \
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

USER workspace
WORKDIR /workspace

LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

CMD ["sleep", "infinity"]
