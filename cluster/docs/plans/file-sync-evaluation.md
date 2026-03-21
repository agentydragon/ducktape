# File Sync Service Evaluation

**Date**: 2026-03-21
**Context**: Replacing Syncthing on the old VPS (being decommissioned) with a
cluster-hosted file sync solution. Key requirements: filesystem-like access,
lazy fetch (on-demand download), selective sync (per-device folder pinning),
and offline capability.

## Solutions Evaluated

### 1. Syncthing (Baseline)

**Architecture**: Peer-to-peer, no central server. Devices sync directly with each
other (or via relay servers when NAT prevents direct connections).

**Selective sync**:
- At the **folder** level: each device chooses which shared folders to sync.
- Within a folder: `.stignore` files with glob patterns. Per-device (not synced).
- No UI for selective sync — must manually craft `.stignore` patterns.
- Workaround for "only sync subfolder X": requires 2 ignore patterns per directory
  level (negate the wanted path, ignore everything else). Fragile.

**Lazy fetch / on-demand**: **Not supported.** All files in a synced folder are
fully downloaded. This is fundamental to the architecture — Syncthing's model is
full bidirectional replication.

**Offline capability**: **Excellent.** All synced files are local. Edits while
offline sync automatically when connectivity resumes. Conflict handling creates
`sync-conflict-<date>` files.

**Conflict resolution**: Creates `<filename>.sync-conflict-<date>-<device>` copies.
No built-in merge. User resolves manually. Conflicts are rare in single-user setups.

**Self-hosted / K8s**: Runs as a single binary. No official Helm chart but trivial
to deploy (StatefulSet + PVC). Already deployed in the old VPS via Ansible.

**Linux client**: Native. Runs as a daemon, CLI + web UI.

**Verdict**: Known-good, but lacks lazy fetch entirely. For large file collections
(photos, media, documents archive), every device must store the full dataset.

---

### 2. Nextcloud (with Virtual Files)

**Architecture**: Client-server. PHP + database server. Heavy (needs Redis, database,
cron, potentially Collabora/OnlyOffice for editing). WebDAV-based sync protocol.

**Selective sync**:
- **Folder-level**: Desktop client allows selecting which top-level folders to sync.
- **Virtual Files mode**: All files visible, downloaded on demand. Per-file granularity.
- Can right-click → "Always keep on this device" (pin) or "Free up space" (unpin).

**Lazy fetch / on-demand**:
- **Windows**: Full VFS support via Windows Cloud Files API. Transparent placeholders.
  Files appear normally in Explorer, downloaded on first access.
- **macOS**: VFS via File Provider API (client 4.0+, Oct 2025). Separate client version.
- **Linux**: **Poor.** Placeholder files get a `.nextcloud` suffix — not transparent.
  No proper VFS integration. Workarounds exist (Flatpak plugin copy hack) but fragile.
  The rclone mount workaround is more reliable but adds complexity.
  [GitHub issue #3668](https://github.com/nextcloud/desktop/issues/3668) tracks this —
  blocked on KDE/GNOME adopting a VFS framework first.

**Offline capability**: Files that have been downloaded (pinned or previously accessed)
are available offline. Edits sync when connectivity returns. Virtual/placeholder files
are **not** available offline.

**Conflict resolution**: Server-side "last write wins" with conflict file copies
(similar to Syncthing). Web UI shows file versions for rollback.

**Self-hosted / K8s**: Official Helm chart exists. Heavy deployment: needs PostgreSQL/
MariaDB, Redis, cron sidecar, and significant RAM (1GB+ minimum). Many moving parts.

**Verdict**: Good VFS on Windows/macOS, but **Linux VFS is effectively broken** — the
`.nextcloud` suffix approach is not transparent. Heavy server. Overkill if only file
sync is needed (Nextcloud is really a groupware platform).

---

### 3. ownCloud Infinite Scale (oCIS)

**Architecture**: Client-server. Single Go binary (no PHP, no web server dependency).
Microservice architecture internally. Supports POSIX, S3, or NFS storage backends.
Much lighter than Nextcloud.

**Selective sync**:
- Desktop client supports traditional folder-level selective sync.
- VFS mode: all files visible as placeholders, per-file pinning.

**Lazy fetch / on-demand**:
- **Windows**: Full VFS support via Windows Cloud Files API. Files appear as
  placeholders, downloaded on access. Windows Storage Sense can auto-evict cached
  files when disk space is low.
- **macOS**: VFS via File Provider API (ownCloud desktop client 5.x+).
- **Linux**: **Limited.** Same situation as Nextcloud — no transparent VFS.
  The ownCloud desktop client on Linux does not have proper kernel-integrated VFS.
  Suffix-based placeholders only.

**Offline capability**: Pinned files available offline. Previously accessed files
cached locally. Non-downloaded placeholders unavailable offline.

**Conflict resolution**: Server-side versioning. Conflict copies created for
simultaneous edits. File locking available.

**Self-hosted / K8s**: Official Helm chart at
[github.com/owncloud/ocis-charts](https://github.com/owncloud/ocis-charts).
Charts are marked "experimental" and must be cloned (not published to a Helm repo).
Single binary, lighter than Nextcloud. Supports S3 backend for blobs.

**Verdict**: Lighter server than Nextcloud, same VFS story (good on Windows/macOS,
poor on Linux). The Go-based architecture is a plus. Helm chart is immature.

---

### 4. Seafile (with SeaDrive)

**Architecture**: Client-server. Python/C server with its own sync protocol (not
WebDAV). More efficient than Nextcloud for pure file sync — uses content-defined
chunking and deduplication. Two client types:
- **Seafile Client**: Traditional full sync (like Syncthing per-folder).
- **SeaDrive**: Virtual drive with on-demand download (the VFS option).

**Selective sync**:
- **Seafile Client**: Folder-level selective sync (choose which libraries to sync).
- **SeaDrive**: All libraries visible as a virtual drive. Per-file/folder pinning
  via right-click → "Always keep on this device" or "Free up space".
- Granularity: individual file or folder level.
- Cache size limit configurable. When exceeded, oldest files evicted first (LRU),
  down to 70% of limit. Pinned files exempt from eviction.

**Lazy fetch / on-demand**:
- **Windows**: Virtual drive via Dokany (user-mode filesystem driver). Files appear
  as placeholders with cloud status icons. Downloaded on first access.
  SeaDrive 3.0 uses Windows Cloud Files API for deeper OS integration.
- **macOS**: Virtual drive via File Provider API (SeaDrive 3.0+). No kernel
  extensions needed.
- **Linux**: **FUSE-based virtual drive.** SeaDrive uses FUSE v2 to mount a virtual
  filesystem at `~/SeaDrive`. Files appear as regular files — `ls` shows all files
  with sizes. File contents fetched on first `read()`. **This is the only solution
  with transparent lazy fetch on Linux.**
- CLI mode available (`SeaDrive-cli`) for headless/server environments.
- File states: placeholder → full (cached) → pinned (always available).

**Offline capability**: **Good.** SeaDrive supports offline mode — cached files
remain accessible when disconnected. Edits to cached files are queued and uploaded
when connectivity resumes. Placeholder files (not yet downloaded) are not accessible
offline. Can pre-pin entire folders for guaranteed offline access.

**Conflict resolution**: "First sync wins" — the version synced to server first stays
unchanged, later versions renamed to `<filename> (SFConflict <author> <timestamp>)`.
File locking available (Pro edition) to prevent conflicts. No built-in merge tool.
File history/versioning available for rollback.

**Self-hosted / K8s**: Official Helm chart at
[github.com/haiwen/seafile-helm-chart](https://github.com/haiwen/seafile-helm-chart).
Supports CE (free), Pro, and Cluster editions. Documented single-node and cluster
deployment modes. Requires MariaDB/MySQL and Memcached/Redis.
Community Helm charts also available (datamate, 300481).

**Verdict**: **Best Linux support** — FUSE-based virtual drive provides transparent
lazy fetch that actually works on Linux. Efficient sync protocol. Mature Helm charts.
The only solution where `ls ~/SeaDrive/MyLibrary/` shows all files without
downloading them, and `cat file.txt` triggers on-demand fetch.

---

### 5. rclone mount

**Architecture**: FUSE mount over any rclone-supported backend (S3, SFTP, WebDAV,
Google Drive, etc.). Not a sync solution per se — more like a network filesystem.

**Selective sync**: **Not built-in.** Can use `--include`/`--exclude` filters on
mount to limit visibility. No per-file pinning or selective sync UI. Would need
manual `rclone sync` commands to pre-populate cache for specific directories.

**Lazy fetch / on-demand**:
- VFS cache mode `full`: on-demand partial file caching. Files fetched from remote
  on first access. Sparse file caching — only accessed byte ranges stored locally.
- Cache size configurable (`--vfs-cache-max-size`, `--vfs-cache-max-age`).
- Read-ahead and prefetch configurable.

**Offline capability**: **Poor.** rclone mount **cannot start offline** even if a
VFS cache exists — this is an open feature request. If the remote is unreachable
during mount, the mount fails. If connectivity drops while mounted, cached files
remain accessible but new file listings fail. No write-back queue for offline edits.

**Conflict resolution**: **None.** rclone mount is not a sync tool. If two machines
mount the same backend, last-write-wins at the storage layer. No conflict detection
or resolution.

**Self-hosted / K8s**: rclone itself doesn't need a server — it connects to existing
storage backends. Could mount an NFS export, S3 bucket, or SFTP server. But you'd
need to deploy the actual storage backend separately.

**Verdict**: Useful as a building block (e.g., mount an S3 bucket) but not a
replacement for a file sync service. No offline support, no conflict resolution,
no sync. Best used in combination with another solution.

---

## Comparison Summary

| Feature              | Syncthing | Nextcloud       | oCIS            | Seafile (SeaDrive) | rclone mount   |
| -------------------- | --------- | --------------- | --------------- | ------------------ | -------------- |
| **Architecture**     | P2P       | Client-server   | Client-server   | Client-server      | FUSE → backend |
| **Linux lazy fetch** | No        | No (`.nc` hack) | No (`.oc` hack) | **Yes (FUSE)**     | Yes (FUSE)     |
| **Win/Mac lazy fetch**| No       | Yes             | Yes             | Yes                | Yes (FUSE)     |
| **Selective sync**   | `.stignore` | Folder + VFS  | Folder + VFS    | Library + per-file | Filters only   |
| **Pin granularity**  | N/A       | Per-file        | Per-file        | Per-file/folder    | N/A            |
| **Offline edits**    | Full      | Pinned only     | Pinned only     | Cached files       | No             |
| **Offline startup**  | Yes       | Yes             | Yes             | Yes                | **No**         |
| **Conflict handling**| Copy      | Copy + versions | Copy + versions | Copy + lock + ver  | None           |
| **Server weight**    | None      | Heavy           | Medium          | Medium             | None (BYO)     |
| **K8s Helm chart**   | Trivial   | Official        | Experimental    | **Official (CE/Pro)** | N/A         |
| **Protocol**         | BEP       | WebDAV          | WebDAV/oCIS     | Custom (efficient) | Various        |
| **License**          | MPL-2.0   | AGPL-3.0        | Apache-2.0      | AGPL-3.0 (CE)      | MIT            |

## Recommendation

**Seafile with SeaDrive** is the strongest candidate for this use case:

1. **Only solution with transparent lazy fetch on Linux** — FUSE-based virtual drive
   where `ls` shows files without downloading them, and access triggers on-demand fetch.
2. **Per-file/folder selective sync** with cache eviction and pinning.
3. **Offline support** — cached files remain accessible, edits queued for upload.
4. **Mature K8s deployment** — official Helm chart with CE, Pro, and Cluster modes.
5. **Efficient sync protocol** — content-defined chunking, better than WebDAV for
   large file collections.

**Runner-up**: If lazy fetch on Linux is not critical (e.g., primarily Windows/macOS
devices), ownCloud Infinite Scale is lighter than Nextcloud and has a cleaner
architecture. But its Helm chart is immature and Linux VFS is not transparent.

**Syncthing** remains a valid option if lazy fetch is not needed and all devices have
enough storage for full replication. Simplest to deploy, most reliable offline.

## Next Steps

1. Deploy Seafile CE on the cluster (official Helm chart on `proxmox-csi-retain`)
2. Test SeaDrive on Linux (FUSE mount, cache behavior, offline resilience)
3. Test selective sync granularity and cache eviction behavior
4. Migrate data from old VPS Syncthing to Seafile
5. Set up SeaDrive on daily-driver machines
6. Evaluate whether Seafile Pro features (file locking, online preview) justify cost

## Sources

- [Seafile SeaDrive Linux client](https://help.seafile.com/drive_client/drive_client_for_linux/)
- [SeaDrive for Windows 10](https://haiwen.github.io/seafile-user-manual/drive_client/drive_client_for_win10/)
- [Seafile Helm chart](https://github.com/haiwen/seafile-helm-chart)
- [Seafile conflict handling](https://help.seafile.com/syncing_client/file_conflicts/)
- [Nextcloud VFS on Linux — GitHub issue #3668](https://github.com/nextcloud/desktop/issues/3668)
- [Nextcloud Virtual Files](https://nextcloud.com/blog/nextcloud-introduces-virtual-drive-in-desktop-client-to-simplify-desktop-integration/)
- [ownCloud VFS docs](https://doc.owncloud.com/desktop/next/vfs.html)
- [ownCloud Infinite Scale](https://owncloud.com/infinite-scale/)
- [oCIS Helm charts](https://github.com/owncloud/ocis-charts)
- [rclone mount VFS cache](https://rclone.org/commands/rclone_mount/)
- [rclone offline mount feature request](https://forum.rclone.org/t/enable-to-mount-remotes-offline-in-case-a-vfs-cache-exists/29821)
- [Syncthing ignore patterns](https://docs.syncthing.net/users/ignoring.html)
- [Syncthing selective sync workaround](https://forum.syncthing.net/t/idea-workaround-for-selective-sync/24192)
