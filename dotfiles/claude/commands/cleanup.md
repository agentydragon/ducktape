# Cleanup

## Description
Sweep through the current working directory to identify and clean up unused files, old experiments, junk, and outdated content.

## Instructions

1. **Analyze the current directory structure**
   - Use `find`, `git status`, and `ls` to understand the codebase
   - Check `.gitignore` to understand what's intentionally untracked
   - Look for common patterns of clutter

2. **Identify cleanup candidates in this order:**

   **Untracked junk files (skip if under dotfiles AND in .gitignore):**
   - Log files (`*.log`, `*.out`, `debug-*`, etc.)
   - Temporary files (`*.tmp`, `*.temp`, `*.swp`, `.DS_Store`)
   - Build artifacts not in `.gitignore`
   - Old test outputs or recordings
   - Cache directories that can be regenerated
   - NOTE: Skip hidden files/directories (starting with .) that are already in .gitignore

   **Redundant downloads/archives:**
   - Downloaded libraries/tools with extracted versions present
   - `.tar.gz`, `.zip` files where contents are already extracted
   - Multiple versions of the same download
   - Well-known libraries that can be easily re-downloaded

   **Old experiments and dead code:**
   - Directories like `old/`, `backup/`, `deprecated/`, `archive/`
   - Files with names like `test-*.js`, `experiment-*`, `poc-*`
   - Commented-out large code blocks
   - Files not imported/required anywhere

   **Outdated documentation:**
   - READMEs referencing non-existent files/directories
   - Documentation for removed features
   - Broken links in markdown files
   - Migration guides for completed migrations

   **Broken references:**
   - Import statements for deleted modules
   - Scripts referencing non-existent files
   - Configuration pointing to missing resources

3. **Present findings organized by category**
   - Group similar items together
   - Show file sizes for large items
   - Indicate if items are git-tracked or not
   - Suggest specific actions for each category

4. **Get user confirmation before deleting**
   - Never auto-delete without permission
   - Offer options: delete all, selective delete, or skip
   - For git-tracked files, suggest `git rm` instead of `rm`

5. **Additional cleanup suggestions:**
   - Consolidate similar configuration files
   - Suggest moving experiments to an `experiments/` directory
   - Recommend adding patterns to `.gitignore`
   - Identify duplicate code/files

## Example Output Format

```
Cleanup Analysis for /path/to/project

Untracked Junk (Safe to delete):
- logs/*.log (15 files, 45MB total)
- workspace-snapshot-*.json (5 files, 125MB)
- old-recording-*.jsonl (3 files, 89MB)
[Skipping .cache/, .npm/, etc - already in .gitignore]

Redundant Archives:
- tana-client.tar.gz (8MB) - already extracted to ./tana-client/
- old-snapshot-2024.json.zip (15MB) - unzipped version exists

Old Experiments:
- experiments/old-auth-flow/ (last modified 3 months ago)
- test-firebase-direct.js (not imported anywhere)
- poc-websocket-handler.ts (superseded by src/client/websocket.ts)

Outdated Documentation:
- docs/old-api.md (references removed endpoints)
- README-deprecated.md (for old version)

Broken References:
- src/index.ts imports './deleted-module'
- scripts/build.sh references tools/old-compiler.js

Suggested actions:
1. Delete all untracked junk files
2. Remove redundant archives
3. Move old experiments to archive/
4. Update or remove outdated docs
5. Fix broken imports

Proceed with cleanup? [y/n/selective]
```

## Key Principles

- Always preserve git history (use `git rm` for tracked files)
- Be conservative - when in doubt, ask
- Explain why something is considered clutter
- Provide size information for large items
- Suggest `.gitignore` updates to prevent future clutter
- Never delete without explicit confirmation
- Skip hidden files/dirs that are already properly gitignored
