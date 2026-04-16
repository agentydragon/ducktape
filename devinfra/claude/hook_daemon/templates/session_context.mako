<%
    from devinfra.claude.hook_daemon.session_start.connectivity import ConnectivityFailed
    status = "ERRORS" if collector.has_errors else "OK with warnings" if collector.has_warnings else "OK"
%>\
# Claude Code session start hook — ${status}

% if connectivity is not None:
**Environment:** ${platform.platform.value} sandbox (direct internet via transparent proxy)
% if isinstance(connectivity, ConnectivityFailed):
**Connectivity:** WARNING — direct probe failed: ${connectivity.reason}.
If this container re-requires explicit proxy configuration (HTTPS_PROXY with JWT,
UDS proxy for Bazel gRPC, Java truststore), restore the `devinfra/claude/auth_proxy/`
subsystem from git history.
% endif
% else:
**Environment:** CLI (local)
% endif
% if startup.exit_code is not None:
<%
    _var_summary = f"yielded {len(startup.env_overlay)} vars"
    if startup.env_overlay:
        _var_summary += f": {', '.join(sorted(startup.env_overlay))}"
%>\
**startup_env_script** `${profile.startup_env_script}` ${"succeeded" if startup.exit_code == 0 else f"FAILED (exit {startup.exit_code})"} — ${_var_summary}
% if startup.output.strip():
```
${startup.output.rstrip()}
```
% endif
% endif
% if container:

## Docker
${container.status}, `DOCKER_HOST=${container.socket_url}`
- `docker run` works. `docker build --network=host` works (BuildKit handles large output gracefully).
  Details: <devinfra/claude/docs/docker_evaluation_results.md>
  Use `--network=host` for builds; Alpine apk may need `--no-check-certificate` for TLS proxy.
- Storage: \
% if container.storage_driver == "overlay":
overlay on tmpfs (layer caching works for <~35 layers). Use `--layers=false` for larger Dockerfiles.
% else:
VFS on 9p (no layer caching, slower builds).
% endif
% endif
% if background_commands:

${"##"} Background tasks
% for cmd in background_commands:
- ${cmd.name}${ " (after env)" if cmd.after_env else "" }
% endfor
% endif
% if buildbuddy_configured:

${"##"} BuildBuddy
Bazel builds and tests by default execute remotely via BuildBuddy.
Use BuildBuddy API (key in `~/.config/bazel/buildbuddy.bazelrc`) to download undeclared test outputs, profiles, search invocations.
% if session_id and session_id != "unknown":
Your `bbr` invocations are tagged `session:${session_id}`. To list your builds:
`bbapi invocation list --tag session:${session_id}`
% endif
% endif
% if collector.has_warnings:

## Warnings
% for record in collector.buffer:
% if record.levelno >= logging.WARNING:
<%
    msg = record.getMessage()
    display_msg = msg[:200] + " [truncated — see log]" if len(msg) > 200 else msg
%>\
- ${record.levelname}: ${display_msg}
% endif
% endfor
% endif

Session start log: `${log_file}`
% if extra_context:
${extra_context}
% endif
