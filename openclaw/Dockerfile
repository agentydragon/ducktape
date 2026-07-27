# Custom OpenClaw image with the OpenShell sandbox plugin and CLI pre-installed.
#
# Extensions live under /app/extensions so the persistent OpenClaw state volume
# cannot hide them. OpenShell is an external package, so we unpack its pinned
# npm artifact into that immutable extension root.
FROM ghcr.io/openclaw/openclaw:2026.7.1
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

USER root

# ── OpenShell plugin (official external OpenClaw plugin) ─────────────────────
ARG OPENCLAW_VERSION=2026.7.1
RUN npm pack \
      --pack-destination /tmp \
      "@openclaw/openshell-sandbox@${OPENCLAW_VERSION}" \
 && mkdir -p /app/extensions/openshell \
 && tar \
      -xzf "/tmp/openclaw-openshell-sandbox-${OPENCLAW_VERSION}.tgz" \
      --strip-components=1 \
      -C /app/extensions/openshell \
 && ln -s /app /app/extensions/openshell/node_modules/openclaw \
 && rm "/tmp/openclaw-openshell-sandbox-${OPENCLAW_VERSION}.tgz" \
 && chown -R node:node /app/extensions/openshell

# ── OpenShell CLI (checksum-pinned static release) ───────────────────────────
ARG TARGETARCH
ARG OPENSHELL_VERSION=0.0.90
RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) \
        openshell_asset="openshell-x86_64-unknown-linux-musl.tar.gz"; \
        openshell_sha256="9f9f1aa379dacb9484a88e0db407e4743d02f02c3c6212187a77064d2334ed35" \
        ;; \
      arm64) \
        openshell_asset="openshell-aarch64-unknown-linux-musl.tar.gz"; \
        openshell_sha256="58b8dad7fae51280446adb322049c162f91541a57cfbbdc0f1f46a46aefcf313" \
        ;; \
      *) \
        echo "unsupported TARGETARCH: ${TARGETARCH}" >&2; \
        exit 1 \
        ;; \
    esac; \
    curl -fLsS \
      "https://github.com/NVIDIA/OpenShell/releases/download/v${OPENSHELL_VERSION}/${openshell_asset}" \
      -o /tmp/openshell.tar.gz; \
    echo "${openshell_sha256}  /tmp/openshell.tar.gz" | sha256sum -c -; \
    tar -xzf /tmp/openshell.tar.gz -C /usr/local/bin openshell; \
    chmod 0755 /usr/local/bin/openshell; \
    rm /tmp/openshell.tar.gz; \
    openshell --version

WORKDIR /app

# Proxy preload: always active. Kyverno injects the proxy env vars at runtime;
# NODE_OPTIONS activates this preload so Node.js uses them from startup.
COPY openclaw/proxy-setup.mjs /app/proxy-setup.mjs
ENV NODE_OPTIONS="--import=file:///app/proxy-setup.mjs"

USER node
