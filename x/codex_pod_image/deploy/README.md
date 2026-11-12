# Codex pod experiment

**Retired and unwired from Flux.** This experiment built a non-root Codex image with
Nix and ran it with a persistent workspace. Its Deployment lacked an OVH node
selector and could be scheduled where its SeaweedFS volume could not attach.
The newer workspace pattern is `cluster/k8s/agents/agent-sandbox/`.

The image source and `.#codex-pod-image` flake output remain, but evaluation fails:
`default.nix` imports `codex-claude.nix` without its required `config` argument.
The image publishing workflow and Flux image automation were removed. The
manifests here are retained as an experiment, not a deployable setup.

The former SSH bootstrap identity and Forgejo `tea` token are parked under
[`cluster/parked/codex-pod/`](../../../cluster/parked/codex-pod/), outside this
Kustomization. The Deployment still expects those credentials. BuildBuddy's key
is no longer approved for this deployment. The general registry pattern is in
[`cluster/docs/container-images.md`](../../../cluster/docs/container-images.md).

## Follow-ups

Before reactivation:

- Replace the parked SOPS credentials with runtime-managed credentials; explicitly
  approve and wire any external credentials the pod needs.
- Fix the image evaluation and volume placement, then restore image publication,
  image automation and a Flux Kustomization.
- If Attic caches are needed, add a rotating Kubernetes Secret/netrc target to
  `cluster/rotators/attic_jwt_rotation`; a manually minted token expires. Public
  keys are in `nix/attic-pubkeys.json`.

Push-time Forgejo package webhook reconciliation was deferred: the provider had
no user-package webhook resource, and polling was adequate for the experiment.
