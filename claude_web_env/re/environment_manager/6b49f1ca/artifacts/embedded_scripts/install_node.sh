#!/bin/bash
set -e

REQUESTED_VERSION=$1
# shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
if [ -z "$REQUESTED_VERSION" ]; then
  echo "Error: Node.js version not specified"
  exit 1
fi

echo "Setting up Node.js $REQUESTED_VERSION symlinks..."

# Parse major version
# shellcheck disable=SC2086 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
MAJOR_VERSION=$(echo ${REQUESTED_VERSION} | cut -d. -f1)

# Node.js installation paths:
#   /opt/node20/bin/node
#   /opt/node21/bin/node
#   /opt/node22/bin/node
#
# Create symlinks to the requested version

NODE_DIR="/opt/node${MAJOR_VERSION}"

# Check if the requested Node.js version is installed
# shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
if [ -d "$NODE_DIR" ] && [ -f "$NODE_DIR/bin/node" ]; then
  echo "Found Node.js ${MAJOR_VERSION} at $NODE_DIR"

  # Create symlinks for node, npm, and npx
  ln -sf "$NODE_DIR/bin/node" /usr/local/bin/node
  echo "Created symlink: /usr/local/bin/node -> $NODE_DIR/bin/node"

  # shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
  if [ -f "$NODE_DIR/bin/npm" ]; then
    ln -sf "$NODE_DIR/bin/npm" /usr/local/bin/npm
    echo "Created symlink: /usr/local/bin/npm -> $NODE_DIR/bin/npm"
  fi

  # shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
  if [ -f "$NODE_DIR/bin/npx" ]; then
    ln -sf "$NODE_DIR/bin/npx" /usr/local/bin/npx
    echo "Created symlink: /usr/local/bin/npx -> $NODE_DIR/bin/npx"
  fi

  echo "Node.js symlink setup completed"

  # Verify symlinks
  if command -v node &>/dev/null; then
    echo "Node version: $(node --version)"
  fi
  if command -v npm &>/dev/null; then
    echo "NPM version: $(npm --version)"
  fi

elif command -v node &>/dev/null; then
  # Fallback: use existing node installation if specific version not found in /opt
  NODE_PATH=$(which node)
  echo "Warning: Node.js ${MAJOR_VERSION} not found in /opt/node${MAJOR_VERSION}"
  echo "Using existing Node.js installation at $NODE_PATH"

  # Check if it's already the right version
  CURRENT_VERSION=$(node --version | cut -d'v' -f2)
  if [[ "$CURRENT_VERSION" == "${REQUESTED_VERSION}"* ]]; then
    echo "Existing Node.js version matches requested version"
  else
    echo "Warning: Existing version ($CURRENT_VERSION) differs from requested (${REQUESTED_VERSION})"
  fi

else
  echo "Error: Node.js ${MAJOR_VERSION} not found in /opt/node${MAJOR_VERSION}"
  echo "Available Node.js installations:"
  ls -la /opt/node*/bin/node 2>/dev/null || echo "No Node.js installations found in /opt/"
  exit 1
fi
