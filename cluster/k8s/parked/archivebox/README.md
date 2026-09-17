# ArchiveBox experimental deployment

ArchiveBox captures web pages as standard WARC, HTML, PDF, screenshot, and
text artifacts. This experiment is currently suspended; its former Authentik
proxy route at `https://archivebox.allegedly.works` has been removed.

The pinned stable ArchiveBox `0.7.4` image uses SQLite; its small, synchronous
state lives on `local-path-ovh`. Bulk captures live at `/data/archive` on an
RWX `seaweedfs-ovh` CSI PVC. This split is deliberate: the cluster's
FUSE-backed SeaweedFS CSI is appropriate for bulk archive artifacts but not
SQLite's fsync-heavy database writes. PostgreSQL support exists upstream only
on ArchiveBox's unreleased development branch, so this experiment does not use
an unpinned development image.

If the experiment is revived, the application can use ArchiveBox's supported
`RemoteUserMiddleware`: Authentik supplies `X-authentik-username`, and a namespace
NetworkPolicy prevents any other pod from reaching the application port and spoofing
that header. The Authentik objects are intentionally retired while the workload is
suspended.

The app Kustomization uses `deletionPolicy: Orphan` while experimental so
removing its Flux controller cannot silently delete captured data. Explicitly
delete the namespace/PVCs only when the archive is intentionally retired.
