// Package shared holds embedded content used by both the anthropic and byoc
// environment types: the default Claude Code settings JSON and the stop hook
// script. Both packages copy these in their init() functions.
//
// Reconstructed from a6f96673 DWARF extraction, carried forward to 495ea204.
// Source path: /home/runner/work/anthropic/anthropic/api-go/environment-manager/internal/envtype/shared/
//
// Key symbols:
//   - shared.DefaultSettingsJSON (0x15adda0)
//   - shared.StopHookScript      (0x15addb0)
//
// Content recovered via runtime observation of the live container: the binary
// writes these files to /home/claude/.claude/ during environment initialization.
// DefaultSettingsJSON is written to .claude/settings.json; StopHookScript is
// written to the path specified by StopHookPath in anthropicConfig (mode 0755).
package shared

// DefaultSettingsJSON is the default Claude Code settings JSON written to
// .claude/settings.json during environment initialization.
// Shared by both the anthropic and byoc environment types.
//
// Content recovered from /home/claude/.claude/settings.json in the live container.
var DefaultSettingsJSON = []byte(`{
    "$schema": "https://json.schemastore.org/claude-code-settings.json",
    "hooks": {
        "Stop": [
            {
                "matcher": "",
                "hooks": [
                    {
                        "type": "command",
                        "command": "~/.claude/stop-hook-git-check.sh"
                    }
                ]
            }
        ]
    },
    "permissions": {
        "allow": ["Skill"]
    }
}`)

// StopHookScript is the stop hook shell script written to the hooks directory
// during environment initialization (mode 0755).
// Shared by both the anthropic and byoc environment types.
//
// Content recovered from /home/claude/.claude/stop-hook-git-check.sh in the live container.
var StopHookScript = []byte(`#!/bin/bash

# Read the JSON input from stdin
input=$(cat)

# Check if stop hook is already active (recursion prevention)
stop_hook_active=$(echo "$input" | jq -r '.stop_hook_active')
if [[ "$stop_hook_active" = "true" ]]; then
  exit 0
fi

# Check if we're in a git repository - bail if not
if ! git rev-parse --git-dir >/dev/null 2>&1; then
  exit 0
fi

no_pr_reminder="Do not create a pull request unless the user has explicitly asked for one."

# Check for uncommitted changes (both staged and unstaged)
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "There are uncommitted changes in the repository. Please commit and push these changes to the remote branch. $no_pr_reminder" >&2
  exit 2
fi

# Check for untracked files that might be important
untracked_files=$(git ls-files --others --exclude-standard)
if [[ -n "$untracked_files" ]]; then
  echo "There are untracked files in the repository. Please commit and push these changes to the remote branch. $no_pr_reminder" >&2
  exit 2
fi

current_branch=$(git branch --show-current)
if [[ -n "$current_branch" ]]; then
  if git rev-parse "origin/$current_branch" >/dev/null 2>&1; then
    # Branch exists on remote - compare against it
    unpushed=$(git rev-list "origin/$current_branch..HEAD" --count 2>/dev/null) || unpushed=0
    if [[ "$unpushed" -gt 0 ]]; then
      echo "There are $unpushed unpushed commit(s) on branch '$current_branch'. Please push these changes to the remote repository. $no_pr_reminder" >&2
      exit 2
    fi
  else
    # Branch doesn't exist on remote - compare against default branch
    unpushed=$(git rev-list "origin/HEAD..HEAD" --count 2>/dev/null) || unpushed=0
    if [[ "$unpushed" -gt 0 ]]; then
      echo "Branch '$current_branch' has $unpushed unpushed commit(s) and no remote branch. Please push these changes to the remote repository. $no_pr_reminder" >&2
      exit 2
    fi
  fi
fi

exit 0
`)
