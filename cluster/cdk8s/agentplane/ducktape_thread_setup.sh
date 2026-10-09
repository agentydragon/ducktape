#!/bin/sh
# Setup runs once per Thread, from its dedicated workspace. Leave an existing checkout
# and its uncommitted changes alone when a Thread is resumed or an operator supplied one.
set -eu
if [ ! -d .git ]; then
  if [ -n "$(ls -A)" ]; then
    echo 'ducktape setup: nonempty directory is not a checkout; refusing to clone over it' >&2
    exit 1
  fi
  git clone --depth 1 --branch devel --single-branch https://github.com/agentydragon/ducktape.git .
fi
if [ "$(git rev-parse --show-toplevel)" != "$PWD" ]; then
  echo 'ducktape setup: expected a checkout rooted in the Thread workspace' >&2
  exit 1
fi
# Uses the repository's default_install_hook_types (pre-commit and prepare-commit-msg).
pre-commit install
