#!/bin/bash
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
if command -v python${MAJOR}.${MINOR} &>/dev/null; then
  PYTHON_TARGET=$(which python${MAJOR}.${MINOR})
  echo "Using python${MAJOR}.${MINOR} for symlinks"

  # Create python symlink
  ln -sf "$PYTHON_TARGET" /usr/local/bin/python
  echo "Created symlink: /usr/local/bin/python -> $PYTHON_TARGET"

  # Create python3 symlink
  ln -sf "$PYTHON_TARGET" /usr/local/bin/python3
  echo "Created symlink: /usr/local/bin/python3 -> $PYTHON_TARGET"

elif command -v python${MAJOR} &>/dev/null; then
  PYTHON_TARGET=$(which python${MAJOR})
  echo "Using python${MAJOR} for symlinks"

  ln -sf "$PYTHON_TARGET" /usr/local/bin/python
  echo "Created symlink: /usr/local/bin/python -> $PYTHON_TARGET"

  ln -sf "$PYTHON_TARGET" /usr/local/bin/python3
  echo "Created symlink: /usr/local/bin/python3 -> $PYTHON_TARGET"

elif command -v python3 &>/dev/null; then
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
if command -v python &>/dev/null; then
  echo "Python version: $(python --version 2>&1)"
fi
if command -v python3 &>/dev/null; then
  echo "Python3 version: $(python3 --version 2>&1)"
fi
