# Transcript import performance

Start with the [Wyrm2 query profile](2026_10_31_import_query_profile.md) for a breakdown of a running importer. The [title projection memory investigation](2026_10_31_title_projection_memory.md) records the importer OOM, the read-only measurements, and the candidate repair for buffered title history.

The [batch publication measurements](2026_11_10_batched_import_publication.md) isolate publication work with a PostgreSQL-backed RBE fixture. The profile and fixture use different workloads. Consult the [archive README](../../README.md) for current import and storage behavior.
