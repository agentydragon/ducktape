# Claude Code session start hook — ${status}

% if proxy:
**Environment:** gVisor sandbox, TLS-inspecting proxy, no overlay fs (vfs), 9p fs
**Bazel:** wrapper adds auth proxy (port ${proxy.port}, ${proxy.ca_status})
% else:
**Environment:** CLI (local)
% endif
% if container:

## ${container.runtime.capitalize()}
${container.status}, `DOCKER_HOST=${container.socket_url}`
% if container.runtime == "docker":
- `docker run` works. `docker build --network=host` works (BuildKit handles large output gracefully).
  Details: <devinfra/claude/docs/docker_evaluation_results.md>
  Use `--network=host` for builds; Alpine apk may need `--no-check-certificate` for TLS proxy.
- Storage: \
% if container.storage_driver == "overlay":
overlay on tmpfs (layer caching works for <~35 layers). Use `--layers=false` for larger Dockerfiles.
% else:
VFS on 9p (no layer caching, slower builds).
% endif
% elif container.runtime == "podman":
- `podman run` works (with `--network=host`). `podman build` works (gVisor workarounds are pre-configured).
  Details: <devinfra/claude/docs/gvisor_dockerfile_build.md>
  RUN steps producing >~3MB stdout may hit a buildah SIGPIPE bug — redirect output if needed.
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
Hook environments installing in background. First `git commit` may block briefly.
% elif isinstance(precommit, PrecommitNotInstalled) or precommit is None:

## pre-commit
**Warning**: pre-commit hook installation failed. Git hooks may not run. Check daemon log for details.
% endif
% if secrets:

## Secrets
${len(secrets.env_vars)} env var(s) loaded from k8s cluster secrets.
% if secrets.kubeconfig_path:
`kubectl` access available: `cluster/k8s/{claude,agent-shared}-rbac/` includes admin in `claude-sandbox` namespace, read-only in `props`.
% endif
% endif
% if buildbuddy_configured:

## BuildBuddy
Bazel builds and tests by default execute remotely via BuildBuddy.
Use BuildBuddy API (key in `~/.config/bazel/buildbuddy.bazelrc`) to download undeclared test outputs, profiles, search invocations.
% endif

% if any(r.levelno >= WARNING for r in log_entries):

## Warnings
% for record in log_entries:
% if record.levelno >= WARNING:
- ${record.levelname}: ${record.getMessage()}
% endif
% endfor
% endif

Session start log: `${log_file}`
% if extra_context:
${extra_context}
% endif
