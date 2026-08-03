# Haku's isolated OpenClaw + Claude Code compatibility spike.
#
# OpenClaw owns chat/session/memory state; Claude Code is the subscription-backed
# agent runtime. The image also carries the same local build/authoring tools as
# the Haku sandbox so the initial agent can reshape its persistent workspace and
# work on haku-state without provisioning a second exec target.
# CLEANUP(added 2026-08-03): move back to the stable channel once 2026.7.2
#   ships stable (npm dist-tag `latest` leaves 2026.7.1-2). This is pinned to a
#   beta because it is the first release that knows Claude Opus 5 exists:
#   2026.7.1 has no `claude-opus-5` row in `CLAUDE_CLI_CONTEXT_WINDOWS`, so the
#   catalog falls back to `DEFAULT_CONTEXT_WINDOW = 2e5` and the session
#   compacts at a fifth of the real window. The beta adds the row at 1e6 and
#   moves `CLAUDE_CLI_DEFAULT_MODEL_REF` to `claude-cli/claude-opus-5`.
#   2026.7.1-1 and -2 are npm-only patches with no image on GHCR, so there is
#   no newer stable tag to take instead.
FROM ghcr.io/openclaw/openclaw:2026.7.2-beta.7
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

USER root

RUN apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      build-essential \
      ca-certificates \
      curl \
      default-jre-headless \
      git \
      jq \
      less \
      openssh-client \
      openssl \
      procps \
      python3 \
      ripgrep \
 && rm -rf /var/lib/apt/lists/*

# Keep this aligned with the other Claude Code images in this repository.
ARG CLAUDE_CODE_VERSION=2.1.198
RUN npm install -g "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" \
 && npm cache clean --force \
 && claude --version

# Haku sandbox tool pins. Language toolchains remain Bazel-managed.
ARG TARGETARCH
ARG BAZELISK_VERSION=v1.25.0
ARG KUBECTL_VERSION=v1.35.1
ARG TEA_VERSION=0.14.0
ARG RUFF_VERSION=0.15.20
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) bazel_arch=amd64; ruff_arch=x86_64 ;; \
      arm64) bazel_arch=arm64; ruff_arch=aarch64 ;; \
      *) echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/bazel \
      "https://github.com/bazelbuild/bazelisk/releases/download/${BAZELISK_VERSION}/bazelisk-linux-${bazel_arch}"; \
    curl -fsSL -o /usr/local/bin/kubectl \
      "https://dl.k8s.io/release/${KUBECTL_VERSION}/bin/linux/${bazel_arch}/kubectl"; \
    curl -fsSL -o /usr/local/bin/tea \
      "https://dl.gitea.com/tea/${TEA_VERSION}/tea-${TEA_VERSION}-linux-${bazel_arch}"; \
    curl -fsSL \
      "https://github.com/astral-sh/ruff/releases/download/${RUFF_VERSION}/ruff-${ruff_arch}-unknown-linux-gnu.tar.gz" \
      | tar -xz -C /usr/local/bin --strip-components=1 --wildcards '*/ruff'; \
    chmod 0755 /usr/local/bin/bazel /usr/local/bin/kubectl /usr/local/bin/tea /usr/local/bin/ruff; \
    bazel --version; \
    kubectl version --client; \
    tea --version; \
    ruff --version

# Node does not honor HTTP(S)_PROXY consistently. Capture an EnvHttpProxyAgent
# before OpenClaw or a channel plugin can replace Undici's global dispatcher.
COPY openclaw/proxy-setup.mjs /app/proxy-setup.mjs
ENV NODE_OPTIONS="--import=file:///app/proxy-setup.mjs" \
    IS_DEMO=1 \
    DISABLE_AUTOUPDATER=1

USER node
WORKDIR /app
