# Custom OpenClaw image with the proxy preload configured.
#
# The preload makes Node honor the proxy environment injected at runtime.
# TODO: Remove this custom image when upstream OpenClaw provides equivalent
# proxy preload support. The old OpenShell and Matrix customizations are retired.
FROM ghcr.io/openclaw/openclaw:2026.7.1
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

WORKDIR /app

# Proxy preload: always active. Kyverno injects the proxy env vars at runtime;
# NODE_OPTIONS activates this preload so Node.js uses them from startup.
COPY openclaw/proxy-setup.mjs /app/proxy-setup.mjs
ENV NODE_OPTIONS="--import=file:///app/proxy-setup.mjs"

USER node
