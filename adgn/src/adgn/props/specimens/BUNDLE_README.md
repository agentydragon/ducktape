# Specimen Bundle System

This directory uses **git bundles** to store specimen snapshots efficiently and immutably.

## Overview

Specimens that reference commits from the ducktape repository are stored in a **shared bundle** (`ducktape-specimens.bundle`) rather than fetching from GitHub each time. This provides:

- **Efficiency**: Share blob/tree objects across specimens (typically 200-500 KB for 10-20 commits)
- **Immutability**: Binary format that's difficult to accidentally modify
- **Reliability**: Specimens work even if commits are removed from branches or remote
- **Verifiability**: `git bundle verify` checks integrity

## Bundle Structure

```
specimens/
├── ducktape-specimens.bundle          # Shared bundle for all ducktape specimens
├── rebuild_ducktape_bundle.sh         # Script to rebuild the bundle
├── 2025-09-02-ducktape_wt/
│   └── manifest.yaml                  # References bundle
└── 2025-09-03-ducktape-llm/
    └── manifest.yaml                  # References same bundle
```

## Manifest Format

Specimens using the bundle have manifests like:

```yaml
source:
  type: bundle
  path: ../ducktape-specimens.bundle
  ref: 0b573cb5f8163b988cd6586cf96f896d8e1c3f09

scope:
  include:
    - 'wt/**'
```

## Rebuilding the Bundle

When adding new ducktape specimens or updating existing ones:

```bash
cd adgn/src/adgn/props/specimens

# Make sure all specimen commits are available locally
# (fetch from remote or checkout branches as needed)

# Rebuild the bundle
./rebuild_ducktape_bundle.sh
```

The script will:
1. Scan all `manifest.yaml` files for ducktape specimens
2. Extract their commit refs
3. Verify commits exist locally
4. Create a new bundle with all commits
5. Verify the bundle integrity

## Manual Bundle Operations

### Create bundle manually

```bash
git bundle create ducktape-specimens.bundle \
  <commit1> <commit2> <commit3> ...
```

### Verify bundle

```bash
git bundle verify ducktape-specimens.bundle
```

### List commits in bundle

```bash
git bundle list-heads ducktape-specimens.bundle
```

### Extract from bundle

```bash
# Clone into a temp directory
git clone ducktape-specimens.bundle temp-checkout

# Or fetch specific commit
git fetch ducktape-specimens.bundle <commit-sha>
```

## Adding a New Ducktape Specimen

1. Create specimen directory with manifest pointing to bundle:

```yaml
source:
  type: bundle
  path: ../ducktape-specimens.bundle
  ref: <new-commit-sha>
```

2. Make sure the commit is available in your local repo:

```bash
git fetch origin  # or whatever branch has it
git cat-file -e <new-commit-sha>  # verify it exists
```

3. Rebuild the bundle:

```bash
./rebuild_ducktape_bundle.sh
```

4. Commit both the manifest and updated bundle

## Implementation Notes

The bundle extraction is handled by `SpecimenRegistry.hydrated_copy()` in `registry.py`:

- Detects `type: bundle` in manifest
- Extracts commit to temporary directory
- Mounts as read-only workspace for evaluation

## Troubleshooting

### "Commit not found in repository"

The commit needs to be in your local git repo before bundling. Try:

```bash
# If commit is on a remote branch
git fetch origin

# If commit is on a local branch
git fetch . branch-name

# If commit is orphaned but in reflog
git reflog | grep <commit-sha>
git checkout <commit-sha>
git branch temp-preserve-commit
```

### "Bundle verification failed"

The bundle file may be corrupted. Rebuild it:

```bash
./rebuild_ducktape_bundle.sh
```

### "Empty bundle"

No ducktape specimens found, or all commits are unreachable. Check:

```bash
# List current specimens
grep -r "repo: ducktape" */manifest.yaml

# Verify commits exist
git cat-file -e <commit-sha>
```
