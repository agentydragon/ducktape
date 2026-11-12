# VM Codex command memory admission

`default.nix` patches the source-built Codex 0.162.1 from the pinned unstable Nixpkgs input.
The exact version assertion makes upgrades review the helper protocol instead of silently
applying an old patch. `package.nix` removes the unused voice host and its media dependencies;
both the production VM and `agentplane-codex-image` use that package.

The Nix images workflow builds the image, checks ordinary `--version` and mandatory admission
failure, and publishes it through its existing devel-only path. The image includes the Nix
runtime closure and `/opt/harnesses/codex/bin/codex`. A VM fixture consumer must pin the published
image digest before enabling `executor_command_memory`. Pods retain their existing harness
and security policy.

The private `CODEX_COMMAND_CGROUP_SOCKET` variable enables the Linux native handshake. The
server joins its reserved cgroup before normal application initialization. Native pipe and PTY
helpers join a command cgroup before decoding the target environment or executing the target.
Root supplies a destination descriptor; the helper writes PID zero, closes the descriptor,
and waits for root's verification and acknowledgement. Required placement never falls back
to direct spawning. The executed tool receives neither the descriptor nor the private variable,
so nested Codex runs normally within the command's existing cgroup.

This patch preserves the upstream helper's argv, exec PID, streams, process group cancellation,
and background descendant behavior. The Runtime owns cap validation, peer authentication,
cgroup permissions, and the enclosing systemd lifetime fence. The kernel fixture exercises
that owner separately; only the VM scenario with this image proves native Session survival
through a real command OOM.
