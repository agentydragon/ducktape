# Langfuse v4 migration recovery

Migration issue [#6956](https://github.com/agentydragon/ducktape/issues/6956) closed on 2026-10-22. Its body records the deployment and validation evidence. Verified at cutover on 2026-10-22: chart `2.1.6`, web/worker `4.67.0`, effective `events_only` / `direct`, normal ClickHouse `8Gi` limits. All five migration steps completed; no temporary retry args or migration env overrides remain.

## Recovery material

The original legacy observations/traces remain intact. Backup `langfuse-before-v4-67-20261022` completed at `05:46:57 UTC` through the existing CNPG barman-cloud integration.

The following local ClickHouse `FREEZE` snapshots on pod `clickhouse/chi-clickhouse-default-0-0-0` (namespace/pod) were deleted with operator approval on 2026-10-22. Both directories were verified absent afterward; filesystem available space increased by 107,017,990,144 bytes (99.7 GiB).

| Path under `/var/lib/clickhouse/shadow/`    | Contents                                                                   |
| ------------------------------------------- | -------------------------------------------------------------------------- |
| `langfuse%2Dv4%2Dbefore%2Dretry%2D20261022` | Before retry, including scratch observations; 137 checksum files verified. |
| `langfuse%2Dbefore%2Dv4%2D67%2D20261022`    | Completed data before the image upgrade; 133 checksum files verified.      |

These were local hardlink snapshots, not off-host backups. Their deletion left live ClickHouse files and the separate PostgreSQL backup untouched. Before/after migration rows and pre-retry DDL are in `/tmp/langfuse-migration-20261022/` on operator workstation `wyrm2`. Pre-upgrade DDL, probe logs, and the full investigation record are in `/tmp/langfuse-cutover-20261022/`.

**Gotcha:** ClickHouse insert progress includes materialized-view work. This retry's `177550` reported writes represented `88775` source observations plus `events_core_mv` writes; progress must not be compared directly with the source part's row count.

## Related incident

The separate LiteLLM liveness restart was traced to a public model-prefix collision and mitigated by renaming the prefix. See the [RCA and generation verification](2026_10_22_litellm_chatgpt_alias_liveness.md).

The workstation artifacts and PostgreSQL backup were not deleted with the ClickHouse snapshots.
