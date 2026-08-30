# ArchiveBox experimental deployment

ArchiveBox captures web pages as standard WARC, HTML, PDF, screenshot, and
text artifacts. This experiment is exposed only through the shared Authentik
proxy at `https://archivebox.allegedly.works`.

The pinned stable ArchiveBox `0.7.4` image uses SQLite; its small, synchronous
state lives on `local-path-ovh`. Bulk captures live at `/data/archive` on an
RWX `seaweedfs-ovh` CSI PVC. This split is deliberate: the cluster's
FUSE-backed SeaweedFS CSI is appropriate for bulk archive artifacts but not
SQLite's fsync-heavy database writes. PostgreSQL support exists upstream only
on ArchiveBox's unreleased development branch, so this experiment does not use
an unpinned development image.

The application relies on ArchiveBox's supported `RemoteUserMiddleware`:
Authentik supplies `X-authentik-username`; a namespace NetworkPolicy prevents
any other pod from reaching the application port and spoofing that header.

The Deployment's idempotent `bootstrap-admin` init container initializes the
collection and makes the declared Authentik principal `agentydragon` a Django
superuser/staff user. It sets an unusable local password, so the account is
entered through Authentik rather than an independently managed ArchiveBox
password. This is expressed in the Deployment instead of being created by an
imperative command in a live Pod.

The app Kustomization uses `deletionPolicy: Orphan` while experimental so
removing its Flux controller cannot silently delete captured data. Explicitly
delete the namespace/PVCs only when the archive is intentionally retired.
