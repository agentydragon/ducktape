# Runtime supplies CODEX_COMMAND_CGROUP_SOCKET only to VM exec-server.
{ codex }:
assert codex.version == "0.162.1";
codex.overrideAttrs (old: {
  patches = (old.patches or [ ]) ++ [ ./command-cgroup.patch ];
})
