# OpenClaw exec sidecar: DirectExecServer + CLI tools for agent use.
#
# Runs DirectExecServer on port 8766 (streamable-http, unauthenticated).
# The OpenClaw gateway plugin connects to this server pod-locally and
# proxies exec tool calls with OPENCLAW_SESSION_ID injection.
#
# Includes mcporter (MCP CLI tool) so the agent can call approval-gated
# MCP tools from the command line, and kubectl for sandbox namespace access.
#
# Built from the approval-gate exec-backend OCI image (contains Python
# runtime + DirectExecServer), extended with Node.js tools.
FROM registry.allegedly.works/approval-gate/exec-backend:latest

USER root

# Install Node.js (for mcporter) and kubectl
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list \
    && curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key \
      | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /" \
      > /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs kubectl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Install mcporter globally
RUN npm install -g mcporter

# Default: run DirectExecServer on 0.0.0.0:8766
# The pod-level security context handles the user.
EXPOSE 8766
USER 1000:1000
