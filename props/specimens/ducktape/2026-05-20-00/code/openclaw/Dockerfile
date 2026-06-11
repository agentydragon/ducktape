# Custom OpenClaw image with:
#   - matrix channel plugin pre-installed
#   - airlock plugin pre-installed
#
# The upstream image bundles extension source at /app/extensions/matrix/ but
# without node_modules/. We install deps directly there so the bundled extension
# loader (discovery position #4) finds them at runtime — no HOME path confusion,
# no init container needed.
#
# Unlike `plugins install` (which uses --ignore-scripts for security), we allow
# postinstall scripts so the crypto native binary downloads automatically.
FROM ghcr.io/openclaw/openclaw:2026.2.24
LABEL org.opencontainers.image.source=https://github.com/agentydragon/ducktape

USER root

# ── Matrix plugin (bundled upstream, needs deps installed) ────────────────────
WORKDIR /app/extensions/matrix

# 1. Strip workspace:* devDependencies (invalid outside the monorepo)
# 2. Install production deps (postinstall scripts download crypto binary)
# 3. Fix ownership for runtime (node user, uid 1000)
RUN node -e " \
  const fs = require('fs'); \
  const p = JSON.parse(fs.readFileSync('package.json', 'utf8')); \
  if (p.devDependencies) { \
    for (const k of Object.keys(p.devDependencies)) { \
      if (p.devDependencies[k].startsWith('workspace:')) delete p.devDependencies[k]; \
    } \
  } \
  fs.writeFileSync('package.json', JSON.stringify(p, null, 2) + '\n');" \
 && npm install --omit=dev \
 && chown -R node:node node_modules

# ── Airlock plugin ────────────────────────────────────────────────────────────
# Plugin source built by Bazel (//airlock/openclaw:plugin_tar).
# CI stages the tar at openclaw/airlock-plugin.tar before build.
WORKDIR /app/extensions/airlock
ADD openclaw/airlock-plugin.tar .
RUN npm install --omit=dev \
 && chown -R node:node .

WORKDIR /app

# Proxy preload: always active. Kyverno injects the proxy env vars at runtime;
# NODE_OPTIONS activates this preload so Node.js uses them from startup.
COPY openclaw/proxy-setup.mjs /app/proxy-setup.mjs
ENV NODE_OPTIONS="--import=file:///app/proxy-setup.mjs"

USER node
