# Supported macOS isolation options (2025)

This repo previously used `sandbox-exec` (seatbelt SBPL) to restrict Jupyter/kernel. Apple deprecated that CLI; on this host, narrow `file-read*` allowlists cause libsandbox to abort (rc=-6/134) pre-trace.

## What we tried (and why it failed)

- Narrow allow-list with SBPL under `(deny default)`:
  - Examples: `(allow file-read* (subpath "/bin"))`, literals, regex, plus `file-map-executable`, `file-read-metadata`, dyld paths (`/System`, `/usr/lib`, `/System/Volumes/Preboot`, `/System/Cryptexes`, `/private/var/db/dyld`).
  - Result: sandbox-exec aborts with rc = -6 or 134; no seatbelt trace, no unified “deny” logs (compiler/runtime abort, not a normal policy deny).
- True compiler errors DO emit readable messages:
  - e.g., `(pattern ...)` → `unbound variable: pattern`; unexpected filter argument → `unexpected sbpl-filter argument`.
  - Therefore the narrow allow failure isn’t a normal syntax/semantic error; it’s an opaque abort.
- Allow “/” or default-allow with denials works, but we rejected both on policy grounds:
  - `(allow file-read* (subpath "/"))` → OK, but too broad.
  - `(allow default)` + `deny file-read*` for most roots → works, but violates our “no default allow” rule.

Given the above, we’re moving to supported alternatives below.

## Dynamic, per-invocation paths — key constraint

- If you need to define allowed read/write paths dynamically at invocation time (e.g., flags like allow-read=/foo, allow-write=/bar for an arbitrary target binary), App Sandbox cannot express that purely via CLI flags. Access must be granted via entitlements plus security-scoped bookmarks obtained with prior user consent or pre-provisioned bookmarks.
- Consequently, a sandbox-exec-style wrapper with dynamic path flags is not achievable with App Sandbox alone.
- Recommendation: For dynamic per-run path policies, use a container/VM wrapper (Docker/Colima/Virtualization.framework) and translate flags to bind mounts (-v /foo:ro, -v /bar:rw). This yields the desired semantics reliably today.

## 1) App Sandbox (entitlements) — recommended when shipping an app/helper

- What: Enable App Sandbox in an app/auxiliary helper with Hardened Runtime + specific entitlements.
- How access is scoped: security-scoped bookmarks (user-approved or pre-provisioned) grant read/write to exact directories at runtime (workspace/run_root/tmp). No wildcard path whitelists; you hold bookmarks.
- Dev story:
  - Create a tiny macOS app (Swift/ObjC) that launches the kernel process.
  - Entitlements: `com.apple.security.app-sandbox=true`, optionally turn off network or add loopback only; no broad file entitlements.
  - Use `NSOpenPanel` (or pre-generated bookmarks) to hand only the required directories to the helper.
  - Codesign locally with ad-hoc or Developer ID; App Store is NOT required for local operation. Changing entitlements requires re-signing, but you can vary which directories are accessible at runtime via bookmarks without changing entitlements.
- Pros: Supported, stable, integrates with macOS UX and TCC.
- Cons: Requires an app bundle; you don’t get arbitrary SBPL, you operate via entitlements + bookmarks.

## 2) Endpoint Security (ES) — deep inspection/deny (system extension)

- What: Build a System Extension that subscribes to ES events (open/exec/mmap) and allows/denies in real time.
- Dev story: Requires ES entitlements, notarization, and user approval in System Settings (Security). No App Store is required, but you must sign and the user must approve loading the extension.
- Pros: Powerful; policy can be enforced per-path at runtime.
- Cons: Heavyweight; significant complexity; out-of-scope for a small tool.

## 3) Virtualization — run the kernel in Linux/macOS VM

- What: Use Virtualization.framework (or Docker Desktop/Colima/Lima which run a Linux VM) and mount only the required host directories (workspace/run_root/tmp) read/write; mount nothing else.
- Dev story:
  - Easiest: Docker Desktop (or Colima) and a minimal image (e.g., `python:slim`). Mount only the needed host dirs. Loopback-only networking. This is a VM under the hood on macOS.
  - Stronger but more code: Virtualization.framework VM with VirtioFS shares of the allowed directories.
- Pros: Strong isolation; fast to adopt with Docker/Colima; fully under our control.
- Cons: Heavier than a process sandbox; some developer ergonomics overhead.

## FAQs

- Do I need App Store to use App Sandbox? No. You can codesign locally (ad-hoc/Developer ID) and run. App Store is not required. Changing entitlements requires re-signing, but changing WHICH folders are accessible can be done via bookmarks at runtime without re-signing.
- Can Endpoint Security run locally? Yes, with a Developer ID and user approval in System Settings to load the system extension. Notarization is typically required outside of dev mode.

## Suggested direction for this project

Short-term (required for dynamic paths; fastest path):

- Run the kernel in a Linux VM container (Docker/Colima). Bind-mount only workspace/run_root/tmp. No other host paths. Loopback-only net.
- We can keep YAML policy and translate:
  - `fs.read_paths` → container mounts (ro) and `fs.write_paths` → container mounts (rw)
  - `net.mode` → container network settings

Longer-term (native macOS):

- Build a tiny App Sandbox wrapper app (`JupyterKernelLauncher.app`) with minimal entitlements and security-scoped bookmarks for the allowed dirs. Launch our Python helper inside this app. This gives Apple-supported per-process FS scoping without SBPL.

### Note: Python environments across host/VM/container

- Do not share a single Python venv across host/VM/container unless OS/ABI/arch/Python minor version and absolute path are identical. macOS↔Linux venvs are incompatible.
- Recommended: rebuild the venv per environment, but share wheels/caches:
  - Persist a per-platform wheelhouse and install from it: e.g., inside the container:
    - pip wheel -r requirements.txt -w /shared/wheelhouse/linux-x86_64
    - pip install --no-index --find-links=/shared/wheelhouse/linux-x86_64 -r requirements.txt
  - Mount a shared cache to speed installs: set PIP_CACHE_DIR=/shared/pip-cache (or UV_CACHE_DIR for uv).

## Minimal Docker proof (manual)

```bash
# If Docker Desktop or Colima is running
# Run echo inside Linux VM with NO host mounts (zero host read):
docker run --rm alpine:3.20 /bin/echo OK

# Bind-mount only a temp dir read-only and tmp read-write
TMP=$(mktemp -d)
WS=$(mktemp -d)
docker run --rm \
  -v "$WS:/ws:ro" \
  -v "$TMP:/tmp:rw" \
  --network=none \
  alpine:3.20 /bin/sh -lc 'ls -la /ws; echo OK'
```

We can wire our wrapper to invoke the kernel image with mounts derived from the YAML policy, preserving your existing test flow while achieving enforced non-root reads.
