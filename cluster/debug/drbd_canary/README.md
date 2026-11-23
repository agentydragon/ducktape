# DRBD canary records

Observations from the retired FILE_THIN canary on the two prepared OVH Talos workers. The
deployed design is [Piraeus block storage](../../docs/piraeus.md).

- [results.md](results.md): host preparation through cleanup.
- [thin_resync.md](thin_resync.md): the first sparse filesystem resync stalled with the generated
  defaults; read it before repeating relocation.
- [auto_diskful/](auto_diskful/README.md): automatic local copying and old-copy cleanup after
  moving only the consuming Pod. Its fixtures need the `local-canary` FILE_THIN pool and the
  `storage.allegedly.works/drbd-canary` node label, which no committed manifest defines.
- [failover/](failover/README.md): node-loss failover of `piraeus-ssd-2-autodiskful` with the HA
  controller enabled.
