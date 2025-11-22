#!/bin/bash
set -euxo pipefail

# Ensure we're on the right branch
git checkout claude/fix-specimen-agent4-v2

# Issue 018 is already done on agent1, let me cherry-pick it
git cherry-pick 3aeb247d

# Done
echo "All fixes applied!"
