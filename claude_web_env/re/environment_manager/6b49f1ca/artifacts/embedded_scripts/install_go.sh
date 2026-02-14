#!/bin/bash
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
  elif command -v go &>/dev/null; then
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

elif command -v go &>/dev/null; then
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

{{template "Prolog" .}}
{{template "StatusTable" .}}
{{template "Epilog" .}}

{{define "Prolog"}}
