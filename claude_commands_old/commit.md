---
description: Intelligently commit changes in a git repo
name: commit
---

Analyze a repository with many unstaged changes and help create clean, logical commits from the chaos.
Organize messy uncommitted changes into clean, logical commits. This command handles everything from a few mixed changes to complete chaos with dozens of modified files.

# ⚠️ Critical Behavior

**ALWAYS PAUSE AND ASK**: After analyzing changes, you MUST pause and ask the user how they want to proceed. Never automatically stage or commit without explicit user direction. Present options clearly and wait for user input.

# Process

1. Run any pre-commit hooks to auto-fix formatting
2. Analyze all changes to find logical groupings
3. **PAUSE and present analysis to user for direction**
4. Stage changes based on user's choice
5. Handle files with mixed changes intelligently

**IMPORTANT**: Always pause after analysis and ask the user how they want to proceed. Never automatically stage or commit without explicit user direction.

# Example Session

## Initial Analysis (Always Pause Here)

```
U: /commit
A: Running pre-commit hooks... fixed some formatting issues.

   I've analyzed your changes. You have 23 files modified with quite a mix of changes.

   I found 5 logical groups:
   1. [config] Environment setup for the auth service (3 files)
   2. [feat] Authentication implementation (8 files)
   3. [refactor] Logging cleanup throughout the codebase (15 files)
   4. [docs] API documentation updates (2 files)
   5. [test] New integration tests (4 files)

   Also noticed some files that probably shouldn't be committed:
   - debug.log, server.log (should these go in .gitignore?)
   - backup-2023-12-01.sql (old backup file)
   - experiment-auth-v2.py (looks like abandoned code)

   What would you like to do? I can create separate commits for each group, combine some of them, or show you the actual changes first.

U: let's do the auth implementation

A: Perfect! I'll prepare the authentication commit.

   This will include:
   - New auth service and models
   - Auth endpoints in the API (I'll only stage the auth-related changes)
   - Authentication middleware
   - Tests for the auth system
   - Config and dependencies

   Here's what I'm thinking for the commit message:
   ```

   feat: Implement authentication service

- Add JWT-based authentication with refresh tokens
- Create login/logout endpoints
- Add middleware for protected routes
- Include auth tests

   ```

   Should I go ahead and stage these changes? Or would you like to see the diff first?

U: go ahead

A: ✅ Done! Created the commit:

   ```

   [main 3f7a9b2] feat: Implement authentication service
    8 files changed, 523 insertions(+), 12 deletions(-)

   ```

   You still have 4 groups of changes left. The logging refactor touches quite a few files (15).
   Would you like to tackle that next, or start with something smaller like the config changes?

```

# Different User Choices

## Example: User wants to review first

```

U: can I see the changes first?

A: Of course! Let me show you what's in each group:

   **Config changes** (Group 1):

- Adding auth settings to app.yaml
- New JWT_SECRET in .env.example
- Auth feature flags in config/features.json

   **Authentication implementation** (Group 2):

- Complete auth service with JWT handling
- Login/logout endpoints
- Auth middleware for route protection
- User session management
- Tests covering all auth flows

   **Logging refactor** (Group 3):

- Replacing 47 print() statements with proper logger calls
- Adding structured logging with context
- New logging config

   Want me to show the actual diffs for any of these?

U: actually, let's combine the config and auth into one commit since they're related

A: That makes sense! The auth config and implementation do belong together.

   I'll stage all 11 files:

- 3 config files with auth settings
- 8 auth implementation files

   How about this commit message:

   ```

   feat: Add authentication service with configuration

- Implement JWT-based authentication
- Add auth configuration to app.yaml
- Create login/logout endpoints
- Add middleware and tests

   ```

   Look good?

```

## Example: User wants all changes in one commit

```

U: just put everything in one commit

A: Hmm, that would combine 5 different types of changes - features, refactoring, docs, and tests.

   The commit would be:

- Authentication implementation (8 files)
- Logging refactor (15 files)
- Config changes (3 files)
- Documentation updates (2 files)
- New tests (4 files)

   That's quite a mix! Are you sure you want everything in one commit? It might make the history harder to follow.

U: you're right, let's split them up

A: Alright. Which would you like to start with?

- The authentication feature (it's self-contained)
- The logging refactor (biggest change, touches many files)
- The config setup (smallest)

   Or I can show you more details about any of these.

```

# Automatic Change Analysis & Smart Staging

The model automatically detects overlapping changes and handles partial staging transparently:

```

U: /commit
A: Analyzing your changes... I found 3 logical change groups:

   📊 **Logging Upgrade** (12 files)

- Replacing print() with structured logging
- Touches: api/endpoints.py, auth/service.py, db/connection.py + 9 others

   ⏱️ **Timeout Handling** (8 files)

- Adding timeout parameters to external calls
- Touches: api/endpoints.py, auth/service.py, db/connection.py + 5 others

   🆕 **User Profile Endpoint** (5 files)

- New /users/profile endpoint with auth
- Touches: api/endpoints.py, auth/service.py + 3 others

   How would you like to organize these commits?

U: let's merge timeout & logging into one commit, then the endpoint separately

A: Perfect! Creating two commits:

   1. Infrastructure improvements (timeout + logging)
   2. User profile endpoint

   Working on commit 1...

   ✓ Staged changes:
     - api/endpoints.py (just logging/timeout changes)
     - auth/service.py (just logging/timeout changes)
     - db/connection.py (all changes)
     - utils/helpers.py (all changes)
     - 12 other files

   Ready to commit. Review changes?

U: yes commit it

A: Created commit: "refactor: improve logging and add timeout handling"

   Now staging the user profile endpoint...

   ✓ Staged changes:
     - api/endpoints.py (just the new /users/profile endpoint)
     - auth/service.py (just the profile permission checks)
     - models/user.py (new profile schema)
     - tests/test_profile.py (new tests)
     - docs/api.md (endpoint documentation)

   Ready for commit 2. Continue?

```

# Features

## Intelligent Grouping

- Detects related changes across files
- Identifies refactoring patterns
- Separates features from cleanup
- Handles files with mixed changes

## Safety Features

- Always preserves index state before changes
- Can restore to any saved state
- Shows preview before committing
- Allows incremental review

## Cleanup Detection

- Identifies likely unwanted files (logs, backups)
- Suggests .gitignore additions
- Detects abandoned experiments
- Finds incomplete features

# Examples

## Auto-Detection of Issues

```

U: /commit
A: Running pre-commit hooks... done.

   I noticed some issues while analyzing:

- Found junk files: debug.log, backup files, temp experiments
- auth.py has mixed changes (auth feature + logging refactor)
- 3 files look like abandoned work

   I'll organize the real changes into clean commits and handle these issues.

```

## Smart Staging with Mixed Files

```

U: let's do the auth feature commit
A: Staging authentication changes...

   Handling files with mixed changes:

- auth.py (staging just auth logic, skipping logging changes)
- api/endpoints.py (staging new endpoints, skipping timeout changes)
- tests/test_auth.py (all changes - only auth-related)

   ✓ Staged auth changes from 8 files
   ✓ Ready to commit the authentication feature

```

## Proactive Cleanup

```

U: /commit
A: Before organizing commits, I found files that shouldn't be tracked:

- debug.log, server.log - 7.4MB (*.log → .gitignore?)
- schema.backup, old_config.backup (*.backup → .gitignore?)
- README~ (vim backup) (*~ → .gitignore?)
- experiment-feature.py (unreferenced)

   Should I clean these up and update .gitignore?

```

# Advanced Features

## Saved States Management

```

U: show saved states
A: Saved index states:

   1. COMMIT_TOOL_SAVED_INDEX_1706234567 (2 hours ago) - 3 files
   2. COMMIT_TOOL_SAVED_INDEX_1706234568 (5 min ago) - empty

   Restore with: git read-tree <state-name>

```

## Custom Commit Creation

```

U: I want to create my own grouping
A: Interactive mode activated. I'll help you build custom commits.

   Select files for your commit:

   1. [ ] auth/service.py
   2. [ ] auth/models.py
   3. [ ] api/endpoints.py (partial)
   4. [ ] tests/test_auth.py

   Enter numbers to toggle (e.g., "1 3"), or "done" when ready:

```

## Planning Complex Commits with Task Agent

For complex scenarios, the model uses a Task agent to plan the approach:

```

U: /commit
A: This looks complex with multiple overlapping changes. Let me analyze the best approach...

Tool: Task
Parameters:
  description: Plan commit strategy
  prompt: |
    Analyze git diff output and plan optimal commit organization:
    1. Identify logical change groups (features, refactors, fixes)
    2. Map which files have overlapping changes
    3. For each overlap, determine which hunks belong to which commit
    4. Plan the staging sequence (which files need partial staging)
    5. Consider user workflow - what order makes most sense
    6. Output concrete plan with exact git commands needed

A: Based on the analysis, here's my recommended approach:

   Found 4 logical change groups with significant overlap in core files.

   **Recommended commit order** (minimizes conflicts):

   1. Infrastructure (logging + error handling) - touches 18 files
   2. Performance optimizations - touches 12 files
   3. New user features - touches 8 files
   4. Documentation updates - touches 5 files

   Files requiring partial staging: api/core.py, models/base.py, utils/helpers.py

   Shall we proceed with this plan?

```

# Integration

- Works with @{#messy-workspace} detection
- Can trigger /cleanup for identified junk
- Respects @{#no-data-loss} by saving states
- Compatible with git hooks and aliases

# Technical Implementation

## Index State Saving

Uses git's tree objects to preserve index state:

```bash
# Save current index as tree object
git write-tree  # Returns tree SHA

# Restore index from tree
git read-tree <tree-sha>
```

## Hunk Analysis & Partial Staging

Behind the scenes, the model handles complex partial staging:

```python
# 1. Analyze which hunks belong to which logical change
# Model examines git diff output and categorizes each hunk

# 2. For files with mixed changes, use git add -p
Tool: Bash
Parameters:
  command: echo -e "y\nn\ny\nn\nq" | git add -p api/endpoints.py
  description: Stage logging/timeout hunks, skip endpoint changes

# 3. Verify correct staging
Tool: Bash
Parameters:
  command: git diff --cached api/endpoints.py
  description: Confirm only intended changes are staged
```

The model automatically:

- Detects which files have overlapping changes
- Determines which hunks belong to each logical commit
- Builds appropriate y/n sequences for git add -p
- Falls back to manual file manipulation if hunks are too intertwined
- Verifies staging accuracy before presenting to user

# Notes

- **ALWAYS PAUSES after analysis** - waits for user direction
- Never commits automatically - always requires confirmation
- Preserves all changes - nothing is lost
- Can be interrupted and resumed
- Works with partial staging (git add -p)
- User controls the flow - chooses what to commit and when

# Context

## git status

!`git status`

## git diff HEAD

!`git diff HEAD`

## git log --oneline 10

!`git log --oneline 10`
