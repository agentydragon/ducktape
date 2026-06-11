---
description: Find violations of /code structure and propose cleanup operations
---

# Organize /code Directory Structure

You are tasked with auditing and organizing the `/code` directory to ensure it follows the `domain.tld/org/repo` convention defined in `/code/CLAUDE.md`.

## Task Overview

1. **Audit the structure**: Examine `/code` and identify any directories that violate the convention
2. **Investigate violations**: For each violating directory:
   - Check if it's a git repository and identify its remote origin
   - Determine the correct location based on the remote URL
   - Check for symlinks in `~/code/` that point to it
3. **Propose operations**: Create a detailed plan of operations needed to fix violations
4. **Execute with approval**: Present the plan to the user and execute if approved

## Convention Rules

According to `/code/CLAUDE.md`:

- **GitHub repos**: `github.com/{org}/{repo}`
- **GitLab repos**: `gitlab.com/{org}/{repo}`
- **Self-hosted Git**: `git.k3s.agentydragon.com/{org}/{repo}` or other domain
- **Local projects**: `local/{owner}/{project}` (for projects without git remotes)
- **Other git hosts**: `{domain.tld}/{org}/{repo}` pattern

## Acceptable Exceptions

These do NOT violate the convention:

- `.claude/` - System directory
- `CLAUDE.md` - Configuration file
- `local/` - Root directory for local projects (if empty or only contains owner subdirectories)
- Hidden dotfiles (`.gitignore`, `.git`, etc.)

## Steps to Execute

### 1. List Top-Level Directories

```bash
ls -la /code
```

### 2. For Each Non-Conforming Directory

Run these checks:

```bash
cd /code/{directory}
git remote -v 2>/dev/null || echo "Not a git repo"
```

### 3. Check for Symlinks

```bash
ls -la ~/code/ | grep "{directory}"
```

### 4. Propose Operations

For each violation, propose ONE of these operations:

**Option A: Move git repo to proper location**

```bash
# Determine correct path from git remote URL
# Move: mv /code/{dir} /code/{domain}/{org}/{repo}
# Update symlink: ln -sf /code/{domain}/{org}/{repo} ~/code/{dir}
```

**Option B: Delete and reclone**
If the local copy seems stale or has issues:

```bash
# Find the git remote URL
# Delete: rm -rf /code/{dir}
# Reclone: git clone {url} /code/{domain}/{org}/{repo}
# Create symlink: ln -sf /code/{domain}/{org}/{repo} ~/code/{dir}
```

**Option C: Move to local/**
For projects without remotes:

```bash
# Move: mv /code/{dir} /code/local/{owner}/{dir}
# Update symlink: ln -sf /code/local/{owner}/{dir} ~/code/{dir}
```

**Option D: Delete if duplicate/stale**
If a proper version exists elsewhere

### 5. Present Plan

Show the user:

- List of violations found
- Proposed operation for each
- Expected outcome
- Any potential issues (broken symlinks, data loss, etc.)

### 6. Execute After Approval

Once the user approves, execute the operations in order.

## Important Notes

- Always check `~/code/` for symlinks that need updating
- Preserve git history when moving repositories
- Check if a repo already exists in the correct location before moving
- Handle the `tana-decomp` case specially if encountered (user may want it at top level)
- Be cautious with deletion - verify with user first

## Output Format

Provide a clear summary with:

1. **Violations Found**: List with current locations
2. **Proposed Operations**: Numbered list of operations
3. **Risk Assessment**: Note any potential issues
4. **Ask for approval**: Wait for user confirmation before executing
