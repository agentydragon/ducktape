# Loki Compactor Init Failure from Corrupt S3 Object — superseded

**Date**: 2026-06-02
**Status**: Superseded — the RCA in the original version of this doc was wrong.

This doc previously described the Loki compactor crashloop on
`init delete store: unexpected EOF` as a single-object SeaweedFS
replication race. That was wrong. The Loki cursor was just the first
symptom of a much broader failure: **every SeaweedFS volume id ≤150
is missing from the cluster** because we deleted three volume-server
local-path PVCs across three node-rename pilots without waiting for
re-replication to converge between them.

Read the correct RCA and blast-radius writeup here:

@2026_06_02_seaweedfs_volume_loss_ovh_rename.md
