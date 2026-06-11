# External System Constraints

This document catalogs external constraints that limit our system design choices. These are limitations imposed by third-party tools, platforms, and infrastructure - not our design decisions.

Understanding these constraints is critical for:

- **System architecture decisions** - Why certain implementations were chosen
- **Debugging issues** - Many "bugs" are actually constraint violations
- **Future development** - What's possible vs. what requires workarounds

---

## 1. Colima Filesystem Constraints

### Issue: Bind Mount Path Restrictions

**Constraint**: Colima VM only mounts specific Mac filesystem paths into the VM:

- `~/Users/$USER` (user home directory) ✅
- `/tmp/colima` (Colima temp directory) ✅
- **All other Mac paths are inaccessible** ❌

**Impact on System Design**:

- All Docker bind mount source paths must be under user home directory
- Cannot bind mount system directories like `/var`, `/tmp`, `/opt`
- Output directories must be validated at runtime

**Implementation**:

```python
# COLIMA FILESYSTEM CONSTRAINT: Ensure all bind mount paths are under user home directory
home_dir = Path.home()
try:
    self._output_dir.resolve().relative_to(home_dir.resolve())
except ValueError:
    raise RuntimeError(
        f"Output directory must be under user home directory for colima compatibility. "
        f"Got: {self._output_dir} (not under {home_dir})"
    )
```

**Symptoms of Violation**:

- Docker containers start successfully with no errors
- Bind mounted directories appear empty inside containers
- Files created in containers don't appear on Mac host
- **Silent failure** - no obvious error messages

**Reference**: [Colima FAQ - Docker Bind Mount Issues](https://github.com/abiosoft/colima/blob/main/docs/FAQ.md#issue-with-docker-bind-mount-showing-empty)

**Files**: `docs/COLIMA.md`, `containerized_claude.py:169-181`

---

## 2. Docker Volume Remount Limitations

### Issue: Immutable Volume Mount Permissions

**Constraint**: Docker does not support changing volume mount permissions on running containers.

**Technical Details**:

- Volume mount permissions are immutable once container is created
- No Docker API exists to modify mount configurations on running containers
- Linux mount namespaces don't allow dynamic permission changes within containers
- Container configuration is determined at creation time and remains fixed

**Impact on System Design**:

- Security remount (RW→RO git volumes) requires complete container recreation
- Container ID changes during remount process
- All running processes in container are terminated
- In-memory state and temporary files not in volumes are lost
- ~2-3 second performance cost for container recreation

**Implementation**:

```python
async def _remount_git_readonly(self):
    old_container_id = self._container.id
    self._container.remove(force=True)  # Complete destruction

    volumes = self._get_container_volumes(git_readonly=True)  # New RO config
    self._container = self._create_container(volumes)        # New container
    self._container.start()
```

**Alternative Approaches Considered**:

1. **Multiple Volume Mounts**: Separate RW/RO volumes - adds complexity
2. **Application-Level Controls**: Software-based permissions - reduces security isolation
3. **Init Container Pattern**: Separate setup/execution containers - over-engineering

**Decision**: Container recreation provides cleanest security isolation with acceptable performance cost.

**Reference**: Docker Engine API documentation - `/containers/{id}/update` endpoint limitations

**Files**: `docs/DEBUGGING.md:441-501`, `containerized_claude.py:558-590`

---

## 3. Docker Buildx Context Isolation

### Issue: Buildx Registry Separation

**Constraint**: Docker buildx maintains its own container-based registry separate from the host Docker daemon.

**Technical Details**:

- Base images built with buildx exist in buildx context only
- Subsequent builds (even with buildx) can't see these images in local registry
- Buildx tries Docker Hub first, not local registry
- `--load` exports to host but subsequent buildx builds still fail

**Impact on System Design**:

- Hybrid build approach required: buildx for base layers, legacy builder for task images
- Base layer builds use `claude-builder` with `docker-container` driver
- Task image builds forced to use legacy Docker builder (`DOCKER_BUILDKIT=0`)

**Implementation**:

```python
# Force legacy builder for task images to access local base images
env = os.environ.copy()
env['DOCKER_BUILDKIT'] = '0'
process = subprocess.Popen(build_cmd, env=env, ...)
```

**Symptoms**:

- Base layers build successfully with buildx + caching
- Task images fail with "image not found" errors
- Error: `failed to solve: base-image:tag: not found`
- Works locally but fails in CI/automated builds

**Files**: `docs/DEBUGGING.md:5-62`

---

## 4. Claude SDK Communication Constraints

### Issue: TTY vs Pipe Communication Requirements

**Constraint**: Claude CLI has different behavior for TTY vs pipe input, causing failures in SDK contexts.

**Technical Details**:

- Claude SDK uses `stdin=PIPE, stdout=PIPE` for JSON communication
- Docker's `-t` flag allocates pseudo-TTY triggering Claude CLI's interactive mode
- Interactive mode fails with "Raw mode not supported" when stdin is a pipe
- Claude CLI expects different communication protocols for TTY vs non-TTY

**Impact on System Design**:

- Must use `docker exec -i` (not `-it`) for Claude CLI communication
- SDK communication requires pipe mode, not interactive terminal mode

**Implementation**:

```bash
# ❌ WRONG - Allocates TTY which triggers interactive mode errors
docker exec -it container /usr/local/bin/claude --input-format stream-json

# ✅ CORRECT - Uses pipes like Claude SDK expects
docker exec -i container /usr/local/bin/claude --input-format stream-json
```

**Files**: `docs/DEBUGGING.md:228-240`

---

## 5. Git Repository Write Constraints

### Issue: Agent Security vs Repository Access

**Constraint**: Git repositories must be mounted read-only for security, but agents may need repository context.

**Technical Details**:

- Agents run with full filesystem access within containers
- Git repositories contain sensitive source code and should be protected
- Agent tasks may legitimately need to read repository structure
- Read-write access creates risk of unintended repository modification

**Impact on System Design**:

- Two-phase container setup: initial RW access for setup, then RO remount
- Pre-task commands run with RW git access for cloning/setup
- Agent execution happens with RO git access for security
- Container recreation required for permission change (see Docker constraint #2)

**Implementation**:

```python
# Initial container with RW git access for setup
volumes = self._get_container_volumes(git_readonly=False)
self._container = self._create_container(volumes)

# Setup tasks (git clones, dependency installation)
await self._run_pre_task_commands()

# Remount as read-only for agent execution (requires container recreation)
await self._remount_git_readonly()
```

**Files**: `containerized_claude.py:412-413`, `docs/DEBUGGING.md:441-501`

---

## 6. Container User Permission Requirements

### Issue: Non-Root Execution for Claude Code

**Constraint**: Claude Code security restrictions require non-root container execution.

**Technical Details**:

- Claude Code SDK has built-in security checks that reject root execution
- Docker containers default to root user unless explicitly configured
- `USER` directive in base Dockerfile can be overridden by derived images
- Container permissions affect file access and authentication setup

**Impact on System Design**:

- `USER 1000` directive required in all task-specific Dockerfiles
- Authentication files must be owned by user 1000, not root
- `docker cp` preserves host ownership, requiring explicit `chown` commands
- Files must be placed in user-writable locations

**Implementation**:

```dockerfile
# In generated task Dockerfile
FROM base-image
# ... task-specific setup ...
# CRITICAL: Ensure container runs as non-root user
USER 1000
```

```bash
# Fix ownership for container user after copying
docker cp file.json container:/home/node/file.json
docker exec --user root container chown 1000:1000 /home/node/file.json
```

**Symptoms of Violation**:

- Claude Code fails with permission errors
- Need `--dangerously-skip-permissions` flag (security risk)
- Authentication setup fails due to file permissions
- Files created with root ownership

**Files**: `docs/DEBUGGING.md:66-112`, `containerized_claude.py:244`

---

## 7. File Exclusion Constraints for Grading

### Issue: Authentication Files Contaminating Evaluation

**Constraint**: Grading systems cannot evaluate authentication directories as they contain sensitive data and are not part of the task solution.

**Technical Details**:

- Authentication directories (`.claude/`, `.local/`, `.aws/`, `.azure/`) are required for agent execution
- These directories contain sensitive credentials and configuration
- Grading systems expect only task-related files
- File collection process must filter out authentication artifacts

**Impact on System Design**:

- Exclusion patterns must be configured to skip authentication directories
- File collection requires filtering before sending to grader
- Pattern matching applied during output collection phase

**Implementation**:

```yaml
# In config.yaml
exclude_patterns:
  - "**/.claude/**"
  - "**/.local/**"
```

**Files**: `config.yaml:89-90`, `docs/DEBUGGING.md:178-209`

---

## Summary

These constraints explain many architectural decisions in the codebase:

1. **Per-task Docker images** - Avoids buildx context isolation issues
2. **Container recreation for security** - Docker volume remount limitations
3. **Colima path validation** - macOS Docker bind mount restrictions
4. **Complex authentication setup** - Multi-service authentication requirements
5. **File exclusion patterns** - Grading system purity requirements
6. **Hybrid build process** - Docker buildx vs legacy builder compatibility
7. **Content truncation** - API token and cost limits
8. **Non-root execution** - Claude Code security requirements

When debugging issues or making architectural changes, always consider whether you're working within or against these external constraints.
