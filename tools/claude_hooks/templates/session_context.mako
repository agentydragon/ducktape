Claude Code session start hook [build: ${build_commit}] — ${status}
Environment: gVisor sandbox, TLS-inspecting proxy, no overlay fs (vfs), 9p fs
Bazel: wrapper adds auth proxy (port ${proxy.port}, ${proxy.ca_status})
% if docker:
Docker: ${docker.status}, DOCKER_HOST=${docker.socket_url}
  `docker run` works. `docker build --network=host` works (BuildKit handles large output gracefully).
  See tools/claude_hooks/docs/docker_evaluation_results.md for details. Note: Use `--network=host` for builds; Alpine apk may need `--no-check-certificate` for TLS proxy.
% if docker.storage_driver == "overlay":
  Storage: overlay on tmpfs (layer caching works for <~35 layers). Use `--layers=false` for larger Dockerfiles.
% else:
  Storage: VFS on 9p (no layer caching, slower builds).
% endif
% endif
% if podman:
Podman: ${podman.status}, DOCKER_HOST=${podman.socket_url}. Use fully qualified image names (docker.io/library/...)
  `podman run` works (with --network=host). `podman build` works (gVisor workarounds are pre-configured).
  See tools/claude_hooks/docs/gvisor_dockerfile_build.md for details. Note: RUN steps producing >~3MB stdout may hit a buildah SIGPIPE bug — redirect output if needed.
% if podman.storage_driver == "overlay":
  Storage: overlay on tmpfs (layer caching works for <~50 layers).
% else:
  Storage: VFS on 9p (no layer caching, slower builds).
% endif
% endif
% if mkcert:
Localhost TLS: $MKCERT_CERT / $MKCERT_KEY (auto-trusted). Use for HTTPS dev servers.
% endif
% if isinstance(precommit, PrecommitInstallingHooks):
pre-commit: hook environments installing in background (pid ${precommit.pid}). First `git commit` may block briefly on pre-commit's flock until done. Log: ~/.cache/claude-hooks/pre-commit-install-hooks.log
% endif
% for record in log_entries:
% if record.levelno >= WARNING:
  ${record.levelname}: ${record.getMessage()}
% endif
% endfor
% if secrets:
% if secrets.skipped_files:
Secrets: ${len(secrets.env_vars)} env var(s) decrypted from age-encrypted component files. Skipped (key mismatch): ${", ".join(secrets.skipped_files)}.
% else:
Secrets: ${len(secrets.env_vars)} env var(s) decrypted from age-encrypted component files.
% endif
% if secrets.kubeconfig:
`kubectl`: configured for `${secrets.kubeconfig.server}` (proxy CA injected).
% endif
% endif
BuildBuddy: API key in ~/.config/bazel/buildbuddy.bazelrc. See <docs/buildbuddy_api.md> for undocumented endpoints (profile download, invocation search, cache scorecard).
Setup log: ${log_file}
% if extra_context:
${extra_context}
% endif
