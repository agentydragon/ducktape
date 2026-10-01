# AI session backup server

This namespace serves the workstations' Restic repositories over HTTPS at
`restic.allegedly.works`. The Rest Server requires HTTP Basic authentication and runs
with `--append-only --private-repos`.

## Workstation contract

Use a repository URL with the source workstation's username as its first path segment:

```text
rest:https://restic.allegedly.works/<username>
```

Restic can also use a subrepository below that path. Set
`RESTIC_REST_USERNAME=<username>` and that workstation's unique
`RESTIC_REST_PASSWORD`; do not put credentials in the URL. The usernames are `wyrm2`,
`rugged`, and `iguana`. Their credentials and the shared Restic encryption password are
in `secrets/shared/session-backup.yaml`, with keys
`restic_rest_username_<host>`, `restic_rest_password_<host>`, and `restic_password`.
That shared file is decryptable on all three workstations, so any of them can restore
another workstation's repository. Escrow `restic_password` in the user's password
manager before enabling the client backup timer.

The client-side SQLite backup tree is
`${XDG_CACHE_HOME}/ducktape/ai-session-backup-sqlite`. To restore, select the source
workstation's repository credentials, inspect snapshots with `restic snapshots`, and
inspect a chosen snapshot with `restic ls <snapshot-id>`. Restore it to a temporary
directory with `restic restore <snapshot-id> --target <temporary-directory>`. Because
the client backs up an absolute path, inspect the restored tree (typically
`home/<user>/.cache/ducktape/ai-session-backup-sqlite`, or the actual configured XDG
cache path beneath `home/<user>`) and copy that directory to
`${XDG_CACHE_HOME}/ducktape/ai-session-backup-sqlite` on the chosen workstation.
Confirm the path with `restic ls` before replacing local files.

The first workstation timer run uploads its current Claude and Codex session trees in
full; later runs add incremental snapshots. The client excludes Claude's
`~/.claude/.credentials.json`, Codex's `auth.json`, `config.toml`, `cache/`, and
`plugin-cache/` under the configured `CODEX_HOME` (by default `~/.codex`), and
live SQLite database files. It uses SQLite's online backup API to copy those
databases into `${XDG_CACHE_HOME}/ducktape/ai-session-backup-sqlite`, which is
included in the snapshot.

The client Restic encryption passphrase is `restic_password`. Keep it separate from
`restic_rest_password_<host>`, which authenticates to this server. The latter values
are unique per host and match the bcrypt entries in
`cluster/k8s/session-backup/rest-server-auth.sops.yaml`.

## Storage and recovery boundary

The primary Rest Server data PVC uses the replicated `seaweedfs-ovh` class. A daily
VolSync/Restic `Direct` backup copies the server data into the separate SeaweedFS bucket
`session-backup-backups`, retaining 7 daily, 4 weekly, and 6 monthly copies. The separate
repository provides an in-cluster versioned copy for recovery from primary repository
loss or logical damage while SeaweedFS remains available. Both copies use the same
SeaweedFS cluster and substrate; cluster-wide SeaweedFS failure is outside this
in-cluster backup boundary.

The outer VolSync backup has its own password in
`cluster/k8s/session-backup/session-backup-volsync-restic-password.sops.yaml`; it is
different from the workstation repository encryption passphrase. Rest Server's
append-only mode prevents workstation clients from pruning snapshots or deleting
repository data. The inner repositories therefore grow until an operator performs
planned server-side maintenance. The outer backup's retention applies to copies of the
server data and does not prune workstation repositories.
