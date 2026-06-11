FROM ubuntu:22.04

# Avoid interactive prompts during build
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies including strace for debugging
RUN apt-get update && apt-get install -y \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    python3 \
    python3-pip \
    tini \
    strace \
    && rm -rf /var/lib/apt/lists/*

# Install Claude Code CLI using native installer
# Note: The native installer will handle all dependencies including Node.js if needed
# Install as root first to get system-wide installation
RUN curl -fsSL https://claude.ai/install.sh | bash

# Ensure claude is accessible in PATH for all users
ENV PATH="/root/.local/bin:/home/node/.local/bin:${PATH}"

## Create generic Claude wrapper for SDK integration
## NOTE: This wrapper may be redundant since we override the SDK's binary path entirely,
## but kept temporarily to:
## 1. Document stdin/JSON stream concerns (avoid TTY/raw mode issues)
## 2. Provide debugging hook point if needed
## 3. Maintain working tested code
## Could simplify to call 'claude' directly in task_claude.py later
#RUN cat > /usr/bin/claude-wrapper << 'EOF'
##!/bin/sh
## Generic Claude wrapper for SDK integration with optional strace debugging
## Direct execution without stdin/stdout interference
#
## Build command line by wrapping base command
#CMDLINE="claude"
#if [ -n "$CLAUDE_STRACE" ]; then
#    CMDLINE="strace -f -o /logs/claude_strace.log ${CMDLINE}"
#fi
#
#exec $CMDLINE "$@"
#EOF
#
#RUN chmod +x /usr/bin/claude-wrapper

# Create user matching Claude Code expectations
RUN useradd -m -u 1000 -s /bin/bash node

# Create workspace directory with proper ownership
RUN mkdir -p /workspace && chown -R node:node /workspace

USER 1000

# Use tini as init system to reap zombie processes
ENTRYPOINT ["/usr/bin/tini", "--"]

# Default command - containers will override this
CMD ["sleep", "infinity"]
