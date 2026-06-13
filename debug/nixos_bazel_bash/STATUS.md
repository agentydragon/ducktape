# NixOS + Bazel + RBE: Status — TOMBSTONE

The durable required-configuration rule (the three flags
`--shell_executable=/bin/bash`, `--incompatible_strict_action_env`,
`--host_action_env`, with env-var scoping rationale) was promoted out of
`debug/` to a discoverable doc:

→ **<../../devinfra/docs/nixos_bazel_rbe.md>** (linked from the root `AGENTS.md`
Bazel Commands section).

The full investigation narrative — verified-working package matrix, issue/fix
log, architecture diagram, genrule PATH gap, and remaining work — remains in
`README.md` in this directory. This `STATUS.md` is kept only as a redirect;
delete it once nothing links here.
