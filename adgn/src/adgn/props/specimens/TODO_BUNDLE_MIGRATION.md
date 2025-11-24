# TODO: Migrate Ducktape Specimens to Bundle

## Status

The git bundle infrastructure is complete and ready, but the following specimens cannot be migrated yet because their commits are not available in the repository:

## Specimens Pending Migration

### 2025-09-02-ducktape_wt
- **Commit**: `0b573cb5f8163b988cd6586cf96f896d8e1c3f09`
- **Current source**: GitHub (github source)
- **Target source**: git-bundle (shared ducktape-specimens.bundle)
- **Status**: Commit not found in local repository

### 2025-09-03-ducktape-llm
- **Commit**: `4ad330138e1dd0de43f86ceb65e037663636e2f9`
- **Current source**: GitHub (github source)
- **Target source**: git-bundle (shared ducktape-specimens.bundle)
- **Status**: Commit not found in local repository

## How to Complete Migration

Once the commits become available (e.g., by fetching from a branch or restoring from backup):

1. Verify commits are available:
   ```bash
   git cat-file -e 0b573cb5f8163b988cd6586cf96f896d8e1c3f09
   git cat-file -e 4ad330138e1dd0de43f86ceb65e037663636e2f9
   ```

2. Run the migration script:
   ```bash
   cd adgn/src/adgn/props/specimens
   ./migrate_to_bundles.sh
   ```

3. Build the bundle:
   ```bash
   ./rebuild_ducktape_bundle.sh
   ```

4. Commit the changes:
   ```bash
   git add ducktape-specimens.bundle */manifest.yaml
   git commit -m "Migrate ducktape specimens to shared bundle"
   ```

## Where to Find the Commits

These commits may be available on:
- An unmerged branch in the remote repository
- A local branch that hasn't been pushed
- Git reflog if they were recently checked out
- A backup or archive

Try:
```bash
# Search all remote branches
git fetch --all
git branch -r --contains 0b573cb5f8163b988cd6586cf96f896d8e1c3f09

# Search reflog
git reflog --all | grep 0b573cb

# List all branches that might contain these commits
git log --all --oneline | grep -C5 "wt\|llm"
```
