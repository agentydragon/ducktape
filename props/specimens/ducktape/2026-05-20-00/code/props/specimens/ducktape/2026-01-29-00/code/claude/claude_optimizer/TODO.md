## Network/Internet Access Control

The `internet_needed` field was removed from the codebase as it was not functionally implemented - it only served as metadata in the database and Docker labels without actually controlling network access.

Future implementation should include optional network isolation via task-level switch.
Problem to solve: claude binary still makes LLM sampling requests from inside container.

## Wrong filtering of directories

```
Repository bundles huge Cargo target/ artifacts (> 80 MB) and lock files but omits src/. Documentation is absent. This is poor hygiene.
```

## Git Repository Storage

Consider switching git repositories from Docker volumes to bind mounts for consistency with workspace/logs mounts.

**Current**: Git repos use Docker volume `claude_shared_git`
**Proposed**: Use bind mounts like workspace and logs

**Benefits**:

- More consistent architecture
- Easier external inspection/debugging
- Simpler file system layout

**Considerations**:

- Historical reasons for volumes (Colima path limitations)
- Not currently needed functionality
- Would require testing with Colima constraints

**Priority**: Low - current implementation works fine
