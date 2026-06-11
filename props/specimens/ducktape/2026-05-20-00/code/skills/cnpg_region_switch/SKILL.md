---
name: cnpg_region_switch
description: Migrate a CNPG PostgreSQL cluster between Proxmox and Hetzner regions via streaming replication with sub-second downtime
---

# CNPG Cross-Region Migration

Migrate a single-instance CNPG PostgreSQL cluster between regions using the Standalone Replica Cluster pattern.

## When to use

- Migrating a database from Proxmox to Hetzner (or vice versa)
- Moving a database to region-appropriate storage
- No HA replicas exist — single-instance cluster only

## Procedure

Follow the runbook at `skills/cnpg_region_switch/RUNBOOK.md` step by step.

Key steps:

1. Create source cluster (if not existing)
2. Create target as streaming replica (`pg_basebackup` + `replica.enabled: true`)
3. Verify replication and data
4. Promote target (`spec.replica.enabled: false`)
5. Verify promoted target (writable, data intact, sequences correct)
6. Update application connection string
7. Delete source cluster

## Important constraints

- Both clusters must use the same PostgreSQL image version
- Promotion is irreversible (standalone replica pattern)
- Namespace must have quota for ≥6 services during migration (3 per cluster)
