# Colima Filesystem Constraints for Docker Bind Mounts

**⚠️ CRITICAL WARNING for macOS Users Running Docker via Colima**

## The Problem

When using Docker bind mounts (`-v` or `--mount`) with Colima on macOS, **bind mounts will silently fail** (appear empty in containers) if the host path is not under specific directories that Colima VM can access.

## Root Cause

Colima VM only mounts specific Mac filesystem paths into the VM:

- **`/Users/$USER`** (user home directory)
- **`/tmp/colima`** (Colima temp directory)

**All other Mac paths are inaccessible to the Colima VM and Docker containers.**

## Symptoms

- Docker containers start successfully with no errors
- Bind mounted directories appear empty inside containers
- Files created in containers don't appear on Mac host
- No obvious error messages - **silent failure**

## Solution

**Always ensure bind mount source paths are under your home directory:**

```bash
# ✅ WORKS - under user home
docker run -v ~/my-project:/workspace my-image

# ❌ FAILS SILENTLY - outside user home
docker run -v /tmp/my-project:/workspace my-image
docker run -v /var/log:/logs my-image
```

## Code Implementation

This codebase includes runtime validation in `task_claude.py` to prevent this issue:

```python
# COLIMA FILESYSTEM CONSTRAINT: Ensure all bind mount paths are under user home directory
# Colima VM only mounts Mac /Users/$USER into VM /Users/$USER - other Mac paths are not accessible
home_dir = Path.home()

try:
    self._output_dir.resolve().relative_to(home_dir.resolve())
except ValueError:
    raise RuntimeError(
        f"Output directory must be under user home directory for colima compatibility. "
        f"Got: {self._output_dir} (not under {home_dir}). "
        f"Colima only mounts Mac {home_dir} -> VM {home_dir}, other paths are inaccessible."
    )
```

## Official Reference

This is documented in the official Colima FAQ:

**Source:** <https://github.com/abiosoft/colima/blob/main/docs/FAQ.md#issue-with-docker-bind-mount-showing-empty>

> "When using docker to bind mount a volume (e.g. using `-v` or `--mount`) from the host where the volume is not contained within `/tmp/colima` or `/Users/$USER`, the container will start without raising any errors but the mapped mountpoint on the container will be empty."

## Alternative Solutions

If you must bind mount paths outside your home directory:

1. **Edit Colima VM configuration** to mount additional paths:

   ```bash
   # Edit colima config
   colima start --edit

   # Add mount in mounts section:
   mounts:
     - location: /some/other/path
       writable: true

   # Restart colima
   colima restart
   ```

2. **Copy files** into your home directory instead of bind mounting

3. **Use Docker volumes** instead of bind mounts for temporary storage

## Detection Command

Test if a path is accessible to Colima:

```bash
# This will be empty if path is inaccessible to Colima VM
docker run --rm -v /path/to/test:/test alpine ls -la /test
```

---

**Remember:** This constraint only affects **Colima on macOS**. Native Docker Desktop and Linux Docker don't have this limitation.
