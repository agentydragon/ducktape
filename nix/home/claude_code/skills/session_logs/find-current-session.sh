#!/bin/bash

# Find Current Claude Code Session Script
# Discovers the most likely current session based on cwd, git branch, and recent activity
#
# NOTE: pwd may change during session lifetime, so we search multiple project directories
# and score candidates based on cwd match, git branch, and recency

set -euo pipefail

# Strategy: Check current pwd AND search all recent sessions across all projects
# since pwd may have changed since session start

# Try current pwd first
PROJECT_DIR=$(pwd | sed 's|/|-|g')
SESSION_DIR=~/.claude/projects/$PROJECT_DIR

# Collect candidate session files from multiple sources
CANDIDATES=""

# 1. Sessions from current pwd's project directory (if exists)
if [ -d "$SESSION_DIR" ]; then
  CANDIDATES=$(find "$SESSION_DIR" -type f -name "*.jsonl" -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -5 | cut -d' ' -f2-)
fi

# 2. Also include very recent sessions from ANY project (last 1 hour)
# This catches sessions where pwd changed during session lifetime
RECENT_SESSIONS=$(find ~/.claude/projects/*/[0-9a-f]*.jsonl -type f -mmin -60 -printf "%T@ %p\n" 2>/dev/null | sort -rn | head -10 | cut -d' ' -f2- || true)

# Combine and deduplicate
CANDIDATES=$(echo -e "$CANDIDATES\n$RECENT_SESSIONS" | grep -v '^$' | sort -u || true)

if [ -z "$CANDIDATES" ]; then
  echo "Error: No session files found" >&2
  exit 1
fi

# Get current git branch for matching
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD 2>/dev/null || echo "")

# Generate distinctive recent context markers for content-based matching
# Look for files we've touched in the last few minutes (very distinctive signal)
RECENT_CONTEXT=""
if [ -d .git ]; then
  RECENT_CONTEXT=$(git diff --name-only HEAD 2>/dev/null | head -5 | tr '\n' '|' || true)
fi

# Check each candidate and score them
BEST_SESSION=""
BEST_SCORE=0

while IFS= read -r session; do
  # Extract session metadata from last entry
  LAST_ENTRY=$(tail -1 "$session" 2>/dev/null)

  if [ -z "$LAST_ENTRY" ]; then
    continue
  fi

  # Extract fields
  SESSION_CWD=$(echo "$LAST_ENTRY" | jq -r '.cwd // empty' 2>/dev/null)
  SESSION_BRANCH=$(echo "$LAST_ENTRY" | jq -r '.gitBranch // empty' 2>/dev/null)
  TIMESTAMP=$(echo "$LAST_ENTRY" | jq -r '.timestamp // empty' 2>/dev/null)

  # Calculate age in seconds
  if [ -n "$TIMESTAMP" ]; then
    TIMESTAMP_EPOCH=$(date -d "$TIMESTAMP" +%s 2>/dev/null || echo 0)
    NOW_EPOCH=$(date +%s)
    AGE_SECONDS=$((NOW_EPOCH - TIMESTAMP_EPOCH))
  else
    AGE_SECONDS=999999
  fi

  # Score this session (higher is better)
  SCORE=0

  # Recency is PRIMARY signal (pwd/cwd may differ due to directory changes during session)
  # Very recent: +1000 points if < 10 seconds old (almost certainly current session)
  # Recent: +100 points if < 60 seconds, +50 if < 300 seconds
  # Somewhat recent: +10 if < 3600 seconds
  if [ $AGE_SECONDS -lt 10 ]; then
    SCORE=$((SCORE + 1000))
  elif [ $AGE_SECONDS -lt 60 ]; then
    SCORE=$((SCORE + 100))
  elif [ $AGE_SECONDS -lt 300 ]; then
    SCORE=$((SCORE + 50))
  elif [ $AGE_SECONDS -lt 3600 ]; then
    SCORE=$((SCORE + 10))
  fi

  # Content-based matching: Check if session contains recently modified files
  # This is highly distinctive - if session references files from git diff, it's likely current
  if [ -n "$RECENT_CONTEXT" ]; then
    # Check last 20 tool uses for any of the recently modified files
    CONTENT_MATCH=$(tail -50 "$session" | grep -E "$RECENT_CONTEXT" | wc -l || echo 0)
    if [ "$CONTENT_MATCH" -gt 0 ]; then
      SCORE=$((SCORE + 500)) # Strong signal - session touched same files
    fi
  fi

  # Git branch match: +25 points (good signal if available)
  if [ -n "$CURRENT_BRANCH" ] && [ "$SESSION_BRANCH" = "$CURRENT_BRANCH" ]; then
    SCORE=$((SCORE + 25))
  fi

  # Exact cwd match to current pwd: +20 points bonus
  # (Lower weight since pwd may have changed during session)
  if [ "$SESSION_CWD" = "$(pwd)" ]; then
    SCORE=$((SCORE + 20))
  fi

  # Update best if this is better
  if [ $SCORE -gt $BEST_SCORE ]; then
    BEST_SCORE=$SCORE
    BEST_SESSION="$session"
  fi
done <<<"$CANDIDATES"

if [ -n "$BEST_SESSION" ]; then
  echo "$BEST_SESSION"
  exit 0
else
  # Fallback: just return the most recently modified
  echo "$CANDIDATES" | head -1
  exit 0
fi
