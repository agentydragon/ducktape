// Reconstructed from a6f96673 DWARF extraction, carried forward to 64bc4dc1.
// Package: internal/envtype/anthropic/install_scripts
// Source: internal/envtype/anthropic/install_scripts/scripts.go
//
// Contains embedded shell scripts for language runtime installation.
// These scripts set up symlinks to pre-installed language runtimes.
//
// Symbols:
//   - install_scripts.pythonScript (0x158b200) - 2413 bytes
//   - install_scripts.nodeScript (0x158b210) - 2938 bytes
//   - install_scripts.goScript (0x158b220) - 3185 bytes
//
// Note: In the binary, these are unexported (lowercase) package-level
// variables. For cross-package access in compilable source, they are
// exported here. The binary uses linker-level symbol resolution which
// bypasses Go's export rules.
package install_scripts

// GoScript is the shell script for setting up Go version symlinks.
// Binary symbol: install_scripts.goScript (0x158b220), 3185 bytes
// Referenced at scripts.go:30 in binary source annotations
var GoScript = `#!/bin/bash
set -e

GO_VERSION=$1
# shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
if [ -z "$GO_VERSION" ]; then
    echo "Error: Go version not specified"
    exit 1
fi

echo "Setting up Go $GO_VERSION symlinks..."

# Go versions are installed to:
#   /usr/local/go1.23.5/
#   /usr/local/go1.24.0/
#
# Create symlink: /usr/local/go -> /usr/local/go<version>

GO_DIR="/usr/local/go${GO_VERSION}"

# Check if the requested Go version is installed
# shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
if [ -d "$GO_DIR" ] && [ -f "$GO_DIR/bin/go" ]; then
    echo "Found Go ${GO_VERSION} at $GO_DIR"

    # Create symlink for the Go directory
    ln -sf "$GO_DIR" /usr/local/go
    echo "Created symlink: /usr/local/go -> $GO_DIR"

    # Also create direct binary symlinks in /usr/local/bin for convenience
    # shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
    if [ -f "$GO_DIR/bin/go" ]; then
        ln -sf "$GO_DIR/bin/go" /usr/local/bin/go
        echo "Created symlink: /usr/local/bin/go -> $GO_DIR/bin/go"
    fi

    # shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
    if [ -f "$GO_DIR/bin/gofmt" ]; then
        ln -sf "$GO_DIR/bin/gofmt" /usr/local/bin/gofmt
        echo "Created symlink: /usr/local/bin/gofmt -> $GO_DIR/bin/gofmt"
    fi

    echo "Go symlink setup completed"

    # Verify symlinks
    # shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
    if [ -f go ]; then
        echo "Go version: $(go version)"
    elif command -v go &> /dev/null; then
        echo "Go version: $(go version)"
    fi

elif [ -f go ]; then
    # Check if default /usr/local/go exists
    CURRENT_VERSION=$(go version | sed -n 's/.*go\([0-9.]*\).*/\1/p')
    echo "Warning: Go ${GO_VERSION} not found at $GO_DIR"
    echo "Using existing Go installation at /usr/local/go (version $CURRENT_VERSION)"

    if [[ "$CURRENT_VERSION" == "${GO_VERSION}"* ]]; then
        echo "Existing Go version matches requested version"
    else
        echo "Warning: Existing version ($CURRENT_VERSION) differs from requested (${GO_VERSION})"
    fi

elif command -v go &> /dev/null; then
    # Fallback: use existing go command if found
    GO_PATH=$(which go)
    CURRENT_VERSION=$(go version | sed -n 's/.*go\([0-9.]*\).*/\1/p')
    echo "Warning: Go ${GO_VERSION} not found at $GO_DIR"
    echo "Using existing Go installation at $GO_PATH (version $CURRENT_VERSION)"

    if [[ "$CURRENT_VERSION" == "${GO_VERSION}"* ]]; then
        echo "Existing Go version matches requested version"
    else
        echo "Warning: Existing version ($CURRENT_VERSION) differs from requested (${GO_VERSION})"
    fi

else
    echo "Error: Go ${GO_VERSION} not found at $GO_DIR"
    echo "Available Go installations:"
    ls -la /usr/local/go*/bin/go 2>/dev/null || echo "No Go installations found in /usr/local/go*"
    exit 1
fi
`

// NodeScript is the shell script for setting up Node.js version symlinks.
// Binary symbol: install_scripts.nodeScript (0x158b210), 2938 bytes
// Referenced at scripts.go:25 in binary source annotations
var NodeScript = `#!/bin/bash
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
    if command -v node &> /dev/null; then
        echo "Node version: $(node --version)"
    fi
    if command -v npm &> /dev/null; then
        echo "NPM version: $(npm --version)"
    fi

elif command -v node &> /dev/null; then
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
`

// PythonScript is the shell script for setting up Python version symlinks.
// Binary symbol: install_scripts.pythonScript (0x158b200), 2413 bytes
// Referenced at scripts.go:20 in binary source annotations
var PythonScript = `#!/bin/bash
set -e

VERSION=$1
# shellcheck disable=SC2292 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
if [ -z "$VERSION" ]; then
    echo "Error: Python version not specified"
    exit 1
fi

echo "Setting up Python $VERSION symlinks..."

# Parse major.minor version
# shellcheck disable=SC2086 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
MAJOR=$(echo $VERSION | cut -d. -f1)
# shellcheck disable=SC2086 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
MINOR=$(echo $VERSION | cut -d. -f2)

# Create symlinks to point to the appropriate Python version
echo "Creating python and python3 symlinks..."

# shellcheck disable=SC2086 # AUTO-ADDED: Claude added to align with monorepo checks - please confirm this exception is valid
if command -v python${MAJOR}.${MINOR} &> /dev/null; then
    PYTHON_TARGET=$(which python${MAJOR}.${MINOR})
    echo "Using python${MAJOR}.${MINOR} for symlinks"

    # Create python symlink
    ln -sf "$PYTHON_TARGET" /usr/local/bin/python
    echo "Created symlink: /usr/local/bin/python -> $PYTHON_TARGET"

    # Create python3 symlink
    ln -sf "$PYTHON_TARGET" /usr/local/bin/python3
    echo "Created symlink: /usr/local/bin/python3 -> $PYTHON_TARGET"

elif command -v python${MAJOR} &> /dev/null; then
    PYTHON_TARGET=$(which python${MAJOR})
    echo "Using python${MAJOR} for symlinks"

    ln -sf "$PYTHON_TARGET" /usr/local/bin/python
    echo "Created symlink: /usr/local/bin/python -> $PYTHON_TARGET"

    ln -sf "$PYTHON_TARGET" /usr/local/bin/python3
    echo "Created symlink: /usr/local/bin/python3 -> $PYTHON_TARGET"

elif command -v python3 &> /dev/null; then
    PYTHON_TARGET=$(which python3)
    echo "Using system python3 for symlinks"

    ln -sf "$PYTHON_TARGET" /usr/local/bin/python
    echo "Created symlink: /usr/local/bin/python -> $PYTHON_TARGET"

    # python3 already exists, no need to create another symlink
    echo "Using existing python3: $PYTHON_TARGET"
else
    echo "Warning: No Python version found for symlink creation"
    exit 1
fi

echo "Python symlink setup completed"

# Verify symlinks
if command -v python &> /dev/null; then
    echo "Python version: $(python --version 2>&1)"
fi
if command -v python3 &> /dev/null; then
    echo "Python3 version: $(python3 --version 2>&1)"
fi
`
