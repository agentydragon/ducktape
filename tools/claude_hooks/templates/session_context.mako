# Claude Code session start hook [build: ${build_commit}] — ${status}

**Environment:** gVisor sandbox, TLS-inspecting proxy, no overlay fs (vfs), 9p fs
**Bazel:** wrapper adds auth proxy (port ${proxy.port}, ${proxy.ca_status})
% if container:

## ${container.runtime.capitalize()}
${container.status}, `DOCKER_HOST=${container.socket_url}`
% if container.runtime == "docker":
- `docker run` works. `docker build --network=host` works (BuildKit handles large output gracefully).
  See `tools/claude_hooks/docs/docker_evaluation_results.md` for details. Note: Use `--network=host` for builds; Alpine apk may need `--no-check-certificate` for TLS proxy.
- Storage: \
% if container.storage_driver == "overlay":
overlay on tmpfs (layer caching works for <~35 layers). Use `--layers=false` for larger Dockerfiles.
% else:
VFS on 9p (no layer caching, slower builds).
% endif
% elif container.runtime == "podman":
- `podman run` works (with `--network=host`). `podman build` works (gVisor workarounds are pre-configured).
  See `tools/claude_hooks/docs/gvisor_dockerfile_build.md` for details. Note: RUN steps producing >~3MB stdout may hit a buildah SIGPIPE bug — redirect output if needed.
- Storage: \
% if container.storage_driver == "overlay":
overlay on tmpfs (layer caching works for <~50 layers).
% else:
VFS on 9p (no layer caching, slower builds).
% endif
% endif
% endif
% if mkcert:

## Localhost TLS
`$MKCERT_CERT` / `$MKCERT_KEY` (auto-trusted). Use for HTTPS dev servers.
% endif
% if isinstance(precommit, PrecommitInstallingHooks):

## pre-commit
Hook environments installing in background (pid ${precommit.pid}). First `git commit` may block briefly on pre-commit's flock until done. Log: `${precommit.log_path}`
% endif
% if secrets:

## Secrets
${len(secrets.env_vars)} env var(s) decrypted from age-encrypted component files.
% if secrets.skipped_files:
Skipped (key mismatch): ${", ".join(secrets.skipped_files)}.
% endif
% if secrets.kubeconfig:
`kubectl`: configured for `${secrets.kubeconfig.server}`, namespace `claude-sandbox` (full admin), read-only in `props`.
% endif
% endif

## BuildBuddy
API key in `~/.config/bazel/buildbuddy.bazelrc`. See <docs/buildbuddy_api.md> for undocumented endpoints (profile download, invocation search, cache scorecard).

% if any(r.levelno >= WARNING for r in log_entries):

## Warnings
% for record in log_entries:
% if record.levelno >= WARNING:
- **${record.levelname}:** ${record.getMessage()}
% endif
% endfor
% endif

**Setup log:** `${log_file}`
% if extra_context:
${extra_context}
% endif
