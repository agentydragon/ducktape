# Flux SOPS Ciphertext Applied Literally (Missing/Incomplete `decryption` Block)

**Date**: 2026-07-04
**Status**: Resolved — structural guard in `cluster/validation` (#2795, strengthened to require `secretRef`)

## Symptom

A Flux `Kustomization` whose built output contains a SOPS-encrypted Secret, but whose
`spec.decryption` is missing or incomplete, makes Flux apply the Secret's `ENC[AES256_GCM,…]`
**ciphertext literally** — silently, with no error in `kubectl get kustomization`. The
consumer gets garbage bytes where it expected a credential: a 401, a bogus netrc, a TF
runner holding a key that decrypts nothing.

## Two shapes of the same bug

1. **No `decryption` block at all** — Flux never tries to decrypt; the rendered Secret's
   values are the raw `ENC[…]` strings.
   - `vm-images-publisher` (#2621) — attic netrc
   - `haku/cloud-agent-tf` (#2504) — anthropic-api-key / haku-token → runner 401
   - `agents/tana-mcp-ro`
   - `harbor-secrets` (#2796, suspended at the time)

2. **`provider: sops` declared but no `secretRef.name`** — Flux is told to decrypt but
   given no age key, so it can't; ciphertext is still applied.
   - `litellm-keys-tf` (#2797)

## Root cause

kustomize-controller decrypts SOPS **only** when the Kustomization declares
`spec.decryption.provider: sops` **and** a `secretRef` naming the key Secret. Omit either
and the controller applies the manifest exactly as kustomize built it — which for a
`.sops.yaml` is the still-encrypted `ENC[…]` payload plus the `sops:` metadata block. There
is no warning: `Ready=True`, no events, the Secret object exists with ciphertext values.

It recurs because the block is easy to forget when adding a _first_ SOPS file to a
kustomization whose other secrets arrive via ESO (vm-images-publisher), and because
`provider` without `secretRef` reads as "mostly there" (litellm-keys-tf).

## Prevention

`cluster/validation/checks.py::check_sops_decryption_blocks` (run by the
`test_cluster_integration` Bazel target in CI) is **build-level**: it builds every active
Flux kustomization and flags any that render a SOPS-encrypted Secret (a `SecretResource`
carrying the `sops:` metadata block) without `decryption.provider: sops` **and** a
non-empty `secretRef.name`. Build-level is what makes it precise — it inspects what Flux
actually applies, so it neither over-counts SOPS files owned by a sibling/child
kustomization nor misses those pulled in via nested kustomize refs. The canonical key
Secret across the cluster is `sops-age-cluster-secrets`.

## Detection tip

A live Secret whose data value is `ENC[AES256_GCM,…]` is this bug: `sops -d` works on the
git file, but the in-cluster value is ciphertext → the owning kustomization lacks (or
shorts) its `decryption` block.
