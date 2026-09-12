# Haku zone-worker image: both zone harnesses (Claude Code CLI for the zai
# zone's Anthropic shape, Codex CLI for the future oai zone's Responses shape)
# plus git and the stdlib-only entrypoint wrapper. Built by
# .github/workflows/haku-zone-worker-image.yml (Dockerfile, not Bazel oci_image:
# the npm CLI installs need a package manager at build time).
#
# The image holds NO credentials: the per-job LiteLLM key and result token
# arrive via the Job's secret (haku/archive/2026_08_multi_agent.md → job lifecycle).
FROM node:24.21.0-bookworm-slim@sha256:713cfbf4a0ac19f40e1bb9919893e126b74a5c8cf5d0623c9f89515c8f74c6fa

RUN apt-get update \
    && apt-get install -y --no-install-recommends git ca-certificates python3 \
    && rm -rf /var/lib/apt/lists/*

RUN npm install -g @anthropic-ai/claude-code@2.1.198 @openai/codex@0.144.1 \
    && npm cache clean --force

COPY entrypoint.py /opt/haku/entrypoint.py

# node:*-slim already owns UID 1000 (user `node`); rename it rather than
# useradd a second UID-1000 user (fails: "UID 1000 is not unique").
RUN usermod --login worker --home /home/worker --move-home node \
    && groupmod --new-name worker node \
    && mkdir -p /workspace /output \
    && chown worker:worker /workspace /output
USER worker
WORKDIR /workspace

LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

ENTRYPOINT ["python3", "/opt/haku/entrypoint.py"]
