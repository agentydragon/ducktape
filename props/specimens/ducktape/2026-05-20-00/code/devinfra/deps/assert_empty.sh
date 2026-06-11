#!/bin/bash
# Verify that the given file is empty (no forbidden dependencies found)

if [[ $# -ne 1 ]]; then
  echo "Usage: $0 <deps_file>"
  exit 1
fi

DEPS_FILE="$1"

if [[ ! -f "$DEPS_FILE" ]]; then
  echo "ERROR: File not found: $DEPS_FILE"
  exit 1
fi

if [[ -s "$DEPS_FILE" ]]; then
  echo "ERROR: Unexpected dependencies found:"
  cat "$DEPS_FILE"
  exit 1
fi
