# Custom OpenClaw image with the Matrix channel plugin and proxy preload.
#
# The preload makes Node honor the proxy environment injected at runtime.
# TODO: Remove this custom image when upstream OpenClaw provides equivalent
# proxy preload support. Matrix remains an external plugin.
FROM ghcr.io/openclaw/openclaw:2026.7.1
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

USER root

# ── Matrix channel plugin (official external OpenClaw plugin) ────────────────
# Matrix is a separate plugin, not part of the core image. Install it in the
# immutable bundled-extension directory so the declarative channel config does
# not depend on a writable state PVC or a runtime npm install. Do not use
# --ignore-scripts here: the Matrix crypto dependency needs its native Linux
# addon installed during the image build, even when E2EE is disabled at runtime.
ARG OPENCLAW_VERSION=2026.7.1
RUN npm pack \
      --pack-destination /tmp \
      "@openclaw/matrix@${OPENCLAW_VERSION}" \
 && mkdir -p /app/dist/extensions/matrix \
 && tar \
      -xzf "/tmp/openclaw-matrix-${OPENCLAW_VERSION}.tgz" \
      --strip-components=1 \
      -C /app/dist/extensions/matrix \
 && cd /app/dist/extensions/matrix \
 && npm install --omit=dev --no-audit --no-fund \
 && ln -s /app /app/dist/extensions/matrix/node_modules/openclaw \
 && rm "/tmp/openclaw-matrix-${OPENCLAW_VERSION}.tgz" \
 && chown -R node:node /app/dist/extensions/matrix
WORKDIR /app

# Proxy preload: always active. Kyverno injects the proxy env vars at runtime;
# NODE_OPTIONS activates this preload so Node.js uses them from startup.
COPY openclaw/proxy-setup.mjs /app/proxy-setup.mjs
ENV NODE_OPTIONS="--import=file:///app/proxy-setup.mjs"

USER node
