---
name: update_container_re
description: Update the Claude Code web container reverse engineering effort. Detects changed binaries, captures new references, runs parallel RE subagents for bindiff/decompilation, updates container snapshot/diff, and refreshes all documentation.
argument-hint: "[--detect-only] [--skip-re] [--skip-diff]"
allowed-tools: Bash, Read, Grep, Glob, Edit, Write, Agent, WebSearch, WebFetch
---

# Claude Code Web Container RE Update

Complete procedure for updating the reverse engineering effort when the live
container has changed. This covers binary updates, full disassembly-based
reverse engineering of deltas, container reconstruction diffing, and
documentation refresh.

**Argument:** `$ARGUMENTS` — optional flags to limit scope.

**Directory:** `claude_web_env/` in the repo root.

**Prerequisite:** Phases 1-3 (detection, binary capture, metadata) require
running inside a Claude Code web container with access to the live binaries.
Phase 5 (container build) works from **any machine** with Docker and network
access — it fetches packages from pinned Ubuntu snapshot archives via
`fetch_debs.py`, not dpkg-repack.

---

## Phase 1: Detection — What Changed?

Compare live binaries against stored references to determine scope.

```bash
# Environment-manager
LIVE_EM_HASH=$(md5sum /opt/env-runner/environment-manager | awk '{print $1}')
REF_EM_HASH=$(zcat claude_web_env/reference/environment-manager.gz | md5sum | awk '{print $1}')
echo "environment-manager: live=$LIVE_EM_HASH ref=$REF_EM_HASH"

# process_api (PID 1)
LIVE_PA_HASH=$(md5sum /proc/1/exe | awk '{print $1}')
REF_PA_HASH=$(zcat claude_web_env/reference/process_api.gz | md5sum | awk '{print $1}')
echo "process_api: live=$LIVE_PA_HASH ref=$REF_PA_HASH"

# Quick version check
/usr/local/bin/environment-manager --version 2>&1
```

If hashes match, the binaries haven't changed — skip binary RE and go to
Phase 5 (container diff) to check for package/config changes only.

Extract key properties of changed binaries:

```bash
# For environment-manager (Go, has DWARF)
file /opt/env-runner/environment-manager
readelf -n /opt/env-runner/environment-manager | grep "Build ID"
go version -m /opt/env-runner/environment-manager | head -5

# For process_api (Rust, stripped)
file /proc/1/exe
readelf -n /proc/1/exe | grep "Build ID"
wc -c < /proc/1/exe  # Size comparison
strings /proc/1/exe | grep 'process_api_20'  # Release string
```

---

## Phase 2: Capture New References

### 2a. Binaries

```bash
# process_api
cp /proc/1/exe /tmp/process_api_new
gzip -c /tmp/process_api_new > claude_web_env/reference/process_api.gz

# environment-manager
cp /opt/env-runner/environment-manager /tmp/env-manager-new
gzip -c /tmp/env-manager-new > claude_web_env/reference/environment-manager.gz
```

### 2b. Version Snapshot

```bash
bazel run //claude_web_env/tools:capture_versions -- \
  > claude_web_env/reference/versions-$(date +%Y-%m-%d).yaml

# Diff against previous
bazel run //claude_web_env/tools:capture_versions -- \
  --diff claude_web_env/reference/versions-YYYY-MM-DD.yaml
```

### 2c. Metadata

```bash
# Sandbox settings
/usr/local/bin/environment-manager print-sandbox-settings \
  > claude_web_env/reference/sandbox-settings.json

# Subcommand help
{
  /usr/local/bin/environment-manager --help 2>&1
  /usr/local/bin/environment-manager --version 2>&1
  /usr/local/bin/environment-manager setup --help 2>&1
  /usr/local/bin/environment-manager orchestrator --help 2>&1
  /usr/local/bin/environment-manager task-run --help 2>&1
  /usr/local/bin/environment-manager poll --help 2>&1
} > claude_web_env/reference/subcommands.txt

# Environment variables
env | grep -E '^(CLAUDE|CODESIGN|MCP_)' | sort \
  > claude_web_env/reference/claude-env-vars.txt
```

---

## Phase 3: Create New RE Directories

RE directories are named by BuildID prefix (first 8 hex chars of the ELF
Build ID). This allows multiple binary versions to coexist.

```bash
NEW_EM_BUILDID=$(readelf -n /tmp/env-manager-new | grep 'Build ID' | awk '{print substr($NF,1,8)}')
NEW_PA_BUILDID=$(readelf -n /tmp/process_api_new | grep 'Build ID' | awk '{print substr($NF,1,8)}')

# Copy old RE as starting point
OLD_EM_DIR=$(ls claude_web_env/re/environment_manager/ | grep -v README)
OLD_PA_DIR=$(ls claude_web_env/re/process_api/ | grep -v README)

cp -r "claude_web_env/re/environment_manager/$OLD_EM_DIR" \
      "claude_web_env/re/environment_manager/$NEW_EM_BUILDID"
cp -r "claude_web_env/re/process_api/$OLD_PA_DIR" \
      "claude_web_env/re/process_api/$NEW_PA_BUILDID"
```

---

## Phase 4: Parallel RE Subagents

**Launch two parallel subagents** — one per changed binary. Each works in
isolation (ideally in a worktree) to avoid conflicts.

### Subagent 1: environment-manager (Go with DWARF)

The Go binary ships with full debug info. Use `go tool objdump` for actual
disassembly — not string-level guessing.

**Census (run in parallel within subagent):**

```bash
BIN=/tmp/env-manager-new

# 1. Dependencies and build flags
go version -m "$BIN"

# 2. DWARF source file list
go tool objdump "$BIN" 2>/dev/null | grep -oP 'TEXT \K\S+' | \
  grep -E '(cmd/|internal/)' | sed 's/\..*//' | sort -u

# 3. Application function count and list
go tool nm "$BIN" | grep -E '^0x[0-9a-f]+ T' | \
  grep -E '(cmd\.|internal/)' > /tmp/em-new-functions.txt

# 4. Application strings
strings "$BIN" | sort -u > /tmp/em-new-strings.txt
```

**Diff against old RE:**

- Compare source file lists (find new/removed Go files)
- Compare function lists (find new/removed/resized functions)
- Compare dependency versions
- Compare embedded content (install scripts, hook templates)

**Reconstruction:** For every new or significantly changed function:

1. `go tool objdump -s 'package.FunctionName' "$BIN"` — full annotated disassembly
2. Read the assembly with DWARF source line references
3. Reconstruct actual Go source from disassembly
4. Annotate with `// Binary: 0xADDRESS`

### Subagent 2: process_api (Stripped Rust)

The Rust binary is stripped — no symbols, no debug info. Must use Ghidra
headless or detailed `objdump -d` analysis.

**Census:**

```bash
BIN=/tmp/process_api_new

# 1. String diff
diff <(strings /tmp/process_api_old | sort -u) \
     <(strings "$BIN" | sort -u) > /tmp/pa-string-diff.txt

# 2. Section comparison
readelf -S "$BIN"

# 3. Rust source paths from panic messages
strings "$BIN" | grep '/build/src/'

# 4. New CLI flags
strings "$BIN" | grep -E '^--(addr|port|max|block|fire|cgr|mem|cpu|oom|control)'
```

**Decompilation:** Use Ghidra headless if available, otherwise careful
`objdump -d` analysis:

1. Map functions via string cross-references (panic paths → source files)
2. For each new function: read actual decompiled C pseudocode
3. Translate to idiomatic Rust guided by known types (serde, clap)
4. Annotate with `/// Decompiled from 0xAAAA..0xBBBB`

### Verification (after each subagent)

1. **Build check**: `bazel build //claude_web_env/re/...` succeeds
2. **String coverage**: All application strings in the new binary appear in RE source
3. **Function coverage**: All DWARF-listed functions (env-manager) have source files
4. **Address annotations**: Every function has binary address annotation

---

## Phase 5: Container Snapshot → Diff

**This phase works from any machine with Docker and network access.**

Update the Dockerfile if the version diff (Phase 2b) revealed changes:

- **Node.js/Bun versions**: Update download URLs
- **npm globals**: Update version pins
- **Go versions**: Update download URLs
- **APT packages**: Update `live-dpkg-versions.txt` and optionally advance
  `SNAPSHOT_DATE` in `fetch_debs.py`

Then fetch packages and rebuild:

```bash
# Fetch .deb packages from pinned Ubuntu snapshot archives (no dpkg-repack needed)
bazel run //claude_web_env/tools:fetch_debs

# Build and diff (also calls fetch_debs automatically)
bazel run //claude_web_env/tools:build_and_diff
```

Review `diff_report.md`. Update `exclusions.yaml` if new runtime artifacts
need exclusion. Update `PLAN.md` with the new diff summary.

---

## Phase 6: Documentation Update

Files to update:

| File                                              | What to update                             |
| ------------------------------------------------- | ------------------------------------------ |
| `claude_web_env/PLAN.md`                          | Diff summary, change history entry         |
| `claude_web_env/docs/environment_discovery.md`    | env-manager version, help, flags, env vars |
| `claude_web_env/docs/container_spec.md`           | Binary info, new capabilities              |
| `claude_web_env/re/environment_manager/README.md` | Target binary table, source tree, CLI docs |
| `claude_web_env/re/process_api/README.md`         | Target binary, new features                |
| `claude_web_env/re/*/NEW_BUILDID/README.md`       | Per-version details                        |
| `claude_web_env/re/*/NEW_BUILDID/PLAN.md`         | Reconstruction status                      |

For `environment_discovery.md`, verify:

- `--version` output
- `--help` for all subcommands
- `print-sandbox-settings` output
- Environment variables: `env | grep -E "^(CLAUDE|CODESIGN|MCP_)"`

---

## Phase 7: Commit

Commit together:

- Updated reference binaries and version snapshot
- New RE directories with reconstructed source
- Updated Dockerfile and diff report
- Updated documentation

---

## Key Principles

- **Binary is ground truth.** Every RE decision traces to binary evidence.
- **Full disassembly, not vibes.** Use `go tool objdump` (Go) or Ghidra (Rust), not string guessing.
- **Parallel subagents** for independent binary RE work.
- **Verify results** — builds compile, strings match, functions covered.
- **Delta-focused** — don't rewrite unchanged code, focus on what changed.
- **BuildID-keyed directories** allow multiple versions to coexist.
- **Documentation shows current state only.** READMEs should describe the current
  binary version without historical change summaries or diff sections. Previous
  versions are preserved in their own BuildID directories but the parent README
  reflects current state. Don't accumulate change history in PLAN.md either — keep
  a single current status.

See `/reverse_engineer` skill for the detailed binary RE methodology.

---

## Appendix: Docker Build Proxy Pitfalls

The gVisor sandbox requires an egress proxy for all network access. Docker
build containers don't inherit host env vars, so the proxy must be passed via
`--build-arg http_proxy=... --build-arg https_proxy=...`.

**Critical issue: SHELL wrapper + eval + proxy URLs.**

The Dockerfile uses a logging SHELL wrapper:

```dockerfile
SHELL ["/bin/bash", "-c", "exec 3>&2; set -euo pipefail; trap '...' ERR; exec > /tmp/build-step.log 2>&1; eval \"$0\""]
```

This wrapper uses `eval "$0"` to execute the actual RUN command. When the proxy
URL contains special characters (JWT tokens with `=`, `+`, `/`, `@`), eval can
corrupt the URL or prevent APT from parsing the proxy config correctly. Symptoms:
`apt-get update` fails with "Temporary failure resolving" even though the proxy
IS reachable (verified via `docker run`).

**Fix: Use plain bash shell for APT-setup layers.** Before any RUN that writes
APT proxy configuration or runs `apt-get update`, switch to:

```dockerfile
SHELL ["/bin/bash", "-euo", "pipefail", "-c"]
```

Then restore the logging wrapper afterward. The APT proxy config is written as:

```bash
printf 'Acquire::http::Proxy "%s";\nAcquire::https::Proxy "%s";\n' \
  "${http_proxy:-}" "${https_proxy:-}" > /etc/apt/apt.conf.d/01proxy
```

**Docker cache key behavior:** Docker excludes predefined proxy build-arg names
(`http_proxy`, `https_proxy`, etc.) from cache keys. This means layer caching
is preserved across sessions with different proxy JWTs. However, it also means
changing the RUN instruction text is the only way to bust cache for these layers
— clearing with `docker builder prune --all -f` is the nuclear option.

**APT version alignment:** The snapshot date pins packages to a specific point
in time, but the base image may have newer package versions. Use
`apt-get dist-upgrade --allow-downgrades` to align before installing `-dev`
packages, which have strict version dependencies on their library counterparts.
