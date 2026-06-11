# OpenClaw Matrix Plugin Investigation

**Date**: 2026-02-24
**Status**: Resolved — custom image approach

## Problems Found and Fixed

### 1. NetworkPolicy Port Mismatch (FIXED, committed)

**Symptom**: `https://openclaw.allegedly.works/` returned "upstream connect error or
disconnect/reset before headers".

**Root cause**: Both `networkpolicy-authentik.yaml` and `networkpolicy-node-ingress.yaml`
allowed ingress on port 18789, but the OpenClaw Service maps port 18789 → targetPort 18790
(nginx gateway-proxy sidecar). NetworkPolicy `port` fields match post-DNAT targetPort.

**Fix**: Changed port to 18790 in both NetworkPolicy files. Committed in `a5b642924`.

### 2. Matrix Plugin — Wrong Approach via `skills` (FIXED, committed)

**Symptom**: Added `skills: ["npm:@openclaw/matrix"]` to the CRD. This did not install
the matrix plugin.

**Root cause**: CRD `skills` field uses `npx -y clawhub install` — this installs MCP
servers/skills from the clawhub registry, NOT channel plugins. Completely different
mechanism. The k8s-operator CRD has no `plugins` field.

**Fix**: Replaced `skills` with `initContainers` using `openclaw plugins install`.
Committed in `bc3b88a67`.

### 3. Init Container Heap OOM (FIXED, committed)

**Symptom**: Init container `install-matrix-plugin` crashed with
`FATAL ERROR: Reached heap limit Allocation failed - JavaScript heap out of memory`.

**Root cause**: Node.js defaults to ~256MB heap in containers. The OpenClaw CLI loads
the full application framework just to run `plugins install`.

**Fix**: Added `--max-old-space-size=1024` to the node command and set container memory
limits to 1536Mi. Committed in `5a99616f9`.

### 4. Init Container Volume Mount Path Mismatch (FIXED, pending push)

**Symptom**: `plugins install` installs to `/home/node/.openclaw/extensions/matrix/`
but the PVC is mounted at `/home/openclaw/.openclaw`. Files go to ephemeral storage.

**Root cause**: The container image runs as user `node` (uid 1000) with home `/home/node/`.
The operator overrides `HOME=/home/openclaw` for the main container, but CRD-specified
init containers don't get this override. So `plugins install` uses the default home
`/home/node/` and writes to `/home/node/.openclaw/extensions/matrix/`, which is NOT the
mounted PVC.

**Fix**: Changed init container `mountPath` from `/home/openclaw/.openclaw` to
`/home/node/.openclaw`. Both containers see the same PVC:

- Init container: `/home/node/.openclaw/` (PVC)
- Main container: `/home/openclaw/.openclaw/` (PVC, HOME overridden by operator)

### 5. Native Crypto Module — Likely Non-Blocking

**Symptom**: Init container logs show plugin "failed to load" with:

```text
Cannot find module '@matrix-org/matrix-sdk-crypto-nodejs-linux-x64-gnu'
Require stack:
- .../node_modules/@matrix-org/matrix-sdk-crypto-nodejs/index.js
```

**Root cause**: The `@matrix-org/matrix-sdk-crypto-nodejs` package (transitive dep of
`@vector-im/matrix-bot-sdk`) uses a platform-specific native binary downloaded via
`node-downloader-helper` at install time. The binary either fails to download in the
container environment or isn't available for the platform.

**Key finding — graceful fallback exists**: The extension code at
`extensions/matrix/src/matrix/client/create-client.ts:67-76` wraps the crypto import
in a try/catch:

```typescript
try {
  const { StoreType } = await import("@matrix-org/matrix-sdk-crypto-nodejs");
  cryptoStorage = new RustSdkCryptoStorageProvider(...);
} catch (err) {
  LogService.warn("MatrixClientLite", "Failed to initialize crypto storage, E2EE disabled:", err);
}
```

And the crypto is only initialized when `params.encryption` is true (`create-client.ts:64`).
Our config has `encryption: false`.

The "failed to load" message during `plugins install` is a **non-fatal verification warning**
— the install still succeeds ("Installed plugin: matrix"). The real test is whether the
main container runtime loading works. With `encryption: false` and the try/catch fallback,
the bot should work without the crypto binary.

**Root cause found**: OpenClaw's `install-package-dir.ts:48` runs
`npm install --omit=dev --silent --ignore-scripts`. The `--ignore-scripts` flag
prevents the `postinstall` script (`download-lib.js`) from running. This script
downloads the pre-built `.node` binary from GitHub releases. Without it, the
napi-rs loader in `index.js` can't find the platform binary.

The bot SDK's `index.js` barrel-exports `CryptoClient.js` (line 25:
`__exportStar(require("./e2ee/CryptoClient"), exports)`), which unconditionally
requires `@matrix-org/matrix-sdk-crypto-nodejs` at the top level. So the ENTIRE
bot SDK fails to load if the native binary is missing.

**Fix**: After `plugins install`, manually download the native binary using wget:

```bash
wget -q -O "$CRYPTO_DIR/matrix-sdk-crypto.linux-x64-gnu.node" \
  "https://github.com/matrix-org/matrix-rust-sdk-crypto-nodejs/releases/download/v0.4.0/matrix-sdk-crypto.linux-x64-gnu.node"
```

The binary URL pattern is:
`https://github.com/matrix-org/matrix-rust-sdk-crypto-nodejs/releases/download/v{version}/matrix-sdk-crypto.linux-x64-gnu.node`

Note: `--ignore-scripts` is intentional for security (prevents arbitrary code
execution during install). The manual download is the correct workaround.

### 6. Init Container Not Idempotent (FIXED, pending push)

**Symptom**: On pod restarts, the init container fails with `plugin already exists:
/home/node/.openclaw/extensions/matrix (delete it first)`.

**Root cause**: The PVC retains files from previous installs. `plugins install` refuses
to overwrite existing installations.

**Fix**: Added `rm -rf /home/node/.openclaw/extensions/matrix` before the install command.

### Dependency chain for the crypto issue

```text
@openclaw/matrix (extension)
  → @vector-im/matrix-bot-sdk@0.8.0-element.3
    → @matrix-org/matrix-sdk-crypto-nodejs@^0.4.0
      → (postinstall) node-downloader-helper downloads platform binary
      → @matrix-org/matrix-sdk-crypto-nodejs-linux-x64-gnu (native addon)
```

The native addon is a Rust-compiled Node.js NAPI module providing Matrix E2EE.
Not needed when `encryption: false`.

## Architecture Context

### OpenClaw Plugin Discovery Order

1. Config paths (`--config-dir`)
2. Workspace extensions
3. Global extensions (`~/.openclaw/extensions/` — resolved relative to HOME)
4. Bundled extensions (`/app/extensions/`)

### What `plugins install` Does

1. Copies extension source from source path to `CONFIG_DIR/extensions/<name>/`
2. Strips `workspace:*` devDependencies from `package.json` (fix in 2026.2.23)
3. Runs `npm install --omit=dev` in the copied directory
4. Attempts to load plugin (non-fatal if it fails)
5. Returns success even if the plugin fails to load (warnings are non-fatal)

### CRD Plugin Mechanisms

| Mechanism       | CRD Field             | What It Does                   | Use For                            |
| --------------- | --------------------- | ------------------------------ | ---------------------------------- |
| Skills          | `spec.skills`         | `npx -y clawhub install <pkg>` | MCP servers from clawhub registry  |
| Init containers | `spec.initContainers` | Custom init containers         | Channel plugins, arbitrary setup   |
| (none)          | —                     | —                              | No built-in `plugins` field in CRD |

### Container Image Details

- **Image**: `ghcr.io/openclaw/openclaw:2026.2.23`
- **User**: `node` (uid 1000), home `/home/node/`
- **Bundled extension**: Source at `/app/extensions/matrix/` (no `node_modules/`)
- **Operator override**: Sets `HOME=/home/openclaw` for main container only
- **Architecture**: x86_64 Linux

### Key Source Files

| File                                                         | Purpose                                  |
| ------------------------------------------------------------ | ---------------------------------------- |
| `extensions/matrix/package.json`                             | Extension deps (bot-sdk, crypto-nodejs)  |
| `extensions/matrix/src/matrix/client/create-client.ts:67-76` | Crypto try/catch fallback                |
| `extensions/matrix/src/matrix/deps.ts:10-18`                 | Bot SDK availability check               |
| `src/plugins/install.ts`                                     | Plugin install logic                     |
| `src/plugins/discovery.ts`                                   | Plugin discovery (4 locations)           |
| `src/infra/install-package-dir.ts`                           | npm install + workspace dep sanitization |

### Upstream Issues

- **#24442** (open): "Matrix plugin fails to load after update - missing npm dependencies"
  - Fix commit `1bd79add8` (2026-02-22): strips `workspace:*` devDeps before npm install
  - Included in version 2026.2.23
  - Does NOT address native crypto module availability
- **#16031**: "@vector-im/matrix-bot-sdk silently dropped on pnpm updates"
- **#20548** (closed): "Bundled extensions fail with Cannot find module on npm/brew installs"

## Final Solution: Custom Image

The init container approach was abandoned in favor of a custom Docker image that bakes the
matrix plugin in at build time. This eliminates all the runtime issues (idempotency, volume
paths, heap limits, crypto binary download).

- **Dockerfile**: `openclaw/Dockerfile` — derives from upstream, installs npm deps
  directly in `/app/extensions/matrix/` (bundled extension path). Runs `npm install` without
  `--ignore-scripts` so the crypto native binary downloads automatically via postinstall.
  Skips `plugins install` entirely to avoid HOME path issues (`USER root` → HOME=/root,
  operator overrides HOME=/home/openclaw at runtime — neither matches /home/node).
- **CI**: `.github/workflows/openclaw-image.yml` — builds and pushes to GHCR
- **Registry**: `ghcr.io/agentydragon/openclaw-matrix`
- **Auto-update**: Flux ImagePolicy + ImageUpdateAutomation updates tag in
  `openclawinstance.yaml` when CI pushes a new image

## Commits

| Commit      | Description                                                        |
| ----------- | ------------------------------------------------------------------ |
| `a5b642924` | NetworkPolicy port fix + skills (skills was wrong, later replaced) |
| `bc3b88a67` | Replace skills with initContainers + image pin to 2026.2.23        |
| `5a99616f9` | Heap fix for init container (--max-old-space-size=1024)            |
| `f30d8b7b0` | Volume mount path fix (/home/node/.openclaw) + notes               |
| (pending)   | Custom image approach: Dockerfile, CI, Flux image automation       |
