# Policy Notes

Policy is explicit-only and platform-agnostic at the schema level.

- Filesystem (fs)
  - read_paths: explicit allowlist (absolute paths); include '/' to allow global reads
  - write_paths: explicit allowlist (absolute paths); include '/' to allow global writes
  - Exec: allowed wherever paths are readable or writeable
- Environment (env)
  - set: key/value map passed to child
  - passthrough: names imported from parent env
- Network (net)
  - mode: loopback (recommended); future: none | all | allowlist | proxy

macOS seatbelt specifics

- We render a seatbelt profile using sandbox-exec named parameters (-D KEY=value) for all paths.
- For each write_paths[i] = $WP_i: `(allow file* (subpath "$WP_i"))`and`(allow process-exec (subpath "$WP_i"))`
- For each read_paths[i] = $RP_i: `(allow file-read* (subpath "$RP_i"))`and`(allow process-exec (subpath "$RP_i"|literal))`
- Base policy allows core process/IPC primitives; networking is not broadly opened; loopback inbound only in our launcher.

Planned tightening (validate in tmux before encoding):

1. Trim read_paths to exact venv/python + site-packages required by your kernel
2. Remove global tmp writes (prefer per-run RUN_ROOT)
3. Reduce mach-lookup/system-socket surface to the minimal needed
4. Consider enabling seatbelt trace for targeted runs and diffing denials
