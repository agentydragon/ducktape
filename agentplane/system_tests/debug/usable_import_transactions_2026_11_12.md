# Usable OCI import transactions

This candidate addresses [#16028](https://github.com/agentydragon/ducktape/issues/16028).
It skips a transaction only when containerd's exact tag and manifest digest match the declared
single-image OCI layout, its native platform content check and overlayfs `IsUnpacked` check pass,
and CRI reports the tag pinned on the layout's config digest. Other images take the existing import
path. A failed check command remains an error. Fresh workload/namespace/database/PKI cleanup is unchanged.

The native check uses `ctr images check --quiet` with server-side name, target digest and pin-label
filters. This command prints only complete, unpacked references and can return success with an empty
list. The implementation consumes that list, rather than treating exit status zero as a hit or parsing
the human-readable table. See [containerd's implementation](https://github.com/containerd/containerd/blob/v2.1.5/cmd/ctr/commands/images/images.go)
and [image filters](https://github.com/containerd/containerd/blob/v2.1.5/core/metadata/adaptors.go).

## Acceptance

[Initial acceptance](https://app.buildbuddy.io/invocation/42b6e5d6-b0e7-4a61-98dc-06a6ab1ee828)
passed cold import, a clean live-baseline attach and repeat import. The later
[expanded acceptance](https://app.buildbuddy.io/invocation/5bcb50a3-9acf-4723-b3c8-725077587c94)
passed same-tag/new-manifest detection, a complete control-plane restart retaining the image store,
missing pin, missing committed root snapshot, and CRI RemoveImage followed by reimport. CRI RemoveImage
is the deletion boundary used by kubelet GC; it is not an induced disk-pressure GC cycle. These runs
used base `4415ac2544aef190cce5596d5ee2e73ba627b56f` plus the candidate patch. Subsequent
assertions also reject a stale CRI config ID and fail if a usable image opens a transaction.

The first expanded attempt failed at the experiment's `ctr images label --replace-all` invocation:
containerd tried to validate an empty target. It had already passed retag and restart assertions.
Using an explicit empty pin label exercises the intended missing-pin state and passed. This was a
probe correction, not importer performance evidence.

## Measurement

The manual `//agentplane/system_tests/suites:test_import_transactions` target runs warmup, control,
fast, fast, control on one physical action. Controls disable only the usable-image shortcut. Every arm
uses ordinary probe settings and recreates workload namespaces, PostgreSQL, credentials and issued
roots, then requires normal cleanup. Existing spans and persistence artifacts retain phase durations,
image skip decisions and identity evidence. The warmup is excluded. This tests warm setup, not a full
CI saving; host/provider load can still vary within one action.

The complete selected Pod/MCP Actions/VM graph is also required to check real later Sandbox usage.
Its queue, input fetch, setup, case, cleanup, CPU, peak memory and requested CU-time must be reported
separately. A causal comparison needs matching source/runfiles CAS identities and comparable baseline
warmth plus counterbalanced physical workers. Different-worker observations remain validation only.

Within seven days after merge, or at the next naturally selected backend head, collect the full graph's
physical actions and baseline outcomes. Compare matched warm and cold cohorts; report last required-gate
feedback and resource cost. Keep #16028 open until this post-merge benefit is measured. Do not add the
warm-setup difference to overlapping import/readiness savings.
