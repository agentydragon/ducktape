# Filer OOM during legacy storage cleanup

During the 2026-11-02 legacy PR-visual cleanup, the initial recursive/list
operation coincided with `seaweedfs-filer-0` being terminated as `OOMKilled`
(exit code `137`). Earlier filer logs show a canceled `listDirectory` request for
`/` with a limit of `100000`.

This evidence records temporal overlap, not a proven causal chain. No allocator
trace or heap profile identifies the allocation that exceeded the filer limit.
Do not attribute the OOM to a specific allocator or claim that the canceled list
request alone caused it.

The cleanup later completed through a local client connected to the S3 gateway by
port-forward, using bounded `DeleteObjects` batches with the listed per-object
ETags. Each batch was journaled before submission and after successful completion;
uncertain outcomes required a fresh listing and reconciliation. The proposed
in-cluster client Job in the runbook was not used or verified during this cleanup.
