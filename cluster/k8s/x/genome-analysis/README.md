# Genome analysis (experimental)

Storage for analyzing the operator's own whole-genome sequencing data (a 30x WGS VCF now, the
CRAM once it lands from Nebula) against reference variant-annotation databases, entirely
offline — no genome data leaves the cluster to a third-party API.

Design discussion and open decisions: [ducktape#4788](https://github.com/agentydragon/ducktape/issues/4788)
(evaluating ANNOVAR/OpenCRAVAT/vcfanno before extending the custom `x/genome_analyzer` tool),
[#4783](https://github.com/agentydragon/ducktape/issues/4783) (VCF input),
[#4784](https://github.com/agentydragon/ducktape/issues/4784) (local database downloads),
[#4785](https://github.com/agentydragon/ducktape/issues/4785) (this storage/placement decision).

**Only storage exists so far.** No analysis Job/Deployment yet — that depends on which tool
#4788 lands on.

## Storage

Both PVCs use `local-path-ovh-ssd`, `ReadWriteOnce`:

- `genome-analysis-modules` (50Gi) — downloaded reference databases (ClinVar/gnomAD/dbNSFP or
  whatever #4788 settles on). Written once, read many times by every future analysis run.
- `genome-analysis-data` (100Gi) — the operator's own VCF/CRAM/indices and analysis outputs.

SSD over the SeaweedFS RWX CSI class deliberately: this is a single-pod, single-writer workload
(one job or one GUI pod at a time), so there's no need for SeaweedFS's cross-node RWX mounts —
which also sidesteps their known reliability issue
([ducktape#4616](https://github.com/agentydragon/ducktape/issues/4616), a mount-service death with
no automatic recovery). `local-path-ovh-ssd` binds to nodes labeled `storage.allegedly.works/tier:
ssd` in the `hil-ovh` zone via the StorageClass's own `allowedTopologies` — no manual node
affinity needed.

**Not actually a separate disk.** `local-path-ovh-ssd`'s nodes (`ovh-ns104952`/`ovh-ns104963`)
share one root filesystem across everything on the node — SeaweedFS's own volume/filer data,
Forgejo's DB, and this — not a dedicated SSD partition. Verified free space before provisioning
(~300GB free per node as of 2026-08-26); worth re-checking before requesting significantly more.

Both Kustomizations use `deletionPolicy: Orphan` — sequencing data and downloaded reference
databases are expensive to reproduce, so removing the Flux controller must never implicitly
delete them.
