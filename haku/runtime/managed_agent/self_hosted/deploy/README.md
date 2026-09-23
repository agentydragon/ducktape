# Runtime B deployment archive

This directory preserves the self-hosted Managed Agent's Kubernetes manifests beside
the Haku component that owns them. Runtime B is parked: the generated Flux Kustomization
is suspended and marked `ducktape.org/parked: "true"`. The suspension prevents further
reconciliation; it does not delete resources Flux applied before it was suspended. This
change does not remove any lingering cluster resources.

The suspended Kustomization still points here through the `haku-managed-agent`
ExternalArtifact. This keeps the archived manifests available for deliberate revival.
The environment key remains SOPS-encrypted in `environment-key.sops.yaml`.

## Reactivation conditions

Do not unsuspend this Kustomization as a standalone change. Reactivation requires an
explicit decision to run Runtime B again, plus all of the following in the same reviewed
plan:

1. Choose the worker image lifecycle. Either restore a CI build/publish workflow and
   Flux image tracking, or deliberately build, publish, and pin an image by hand. The
   `.#haku-managed-agent-image` output and recipe remain available for manual builds.
   The Haku NixOS system is excluded from routine Attic targets; flake-specific image
   outputs are not part of that target set.
2. Confirm the environment key, environment ID, Forgejo clone credentials, image-pull
   credentials, and the upstream/shared MCP dependencies are valid.
3. Remove the parked annotation, set `suspend: false`, and verify the Flux path,
   decryption key, dependencies, and selected image before allowing reconciliation.
4. Run a deployment smoke test and confirm the worker can execute and report a tool
   result before treating Runtime B as active.

The environment key was generated in the Anthropic Console, not by the API. To rotate
it after an explicit reactivation, update the `environment_key` field in
`environment-key.sops.yaml` with `sops` and coordinate the value with the environment.
The path is covered by the repository's SOPS rule for cluster deployment secrets.

The worker clones the ducktape mirror at `git.allegedly.works/agentydragon/ducktape`,
not GitHub, because the haku-sandbox egress proxy blocks GitHub. That mirror is bumped
manually. Confirm it contains the intended `haku/base` and `haku/run.md` before any
reactivated run.

The `haku` Forgejo user's read grants on `agentydragon/ducktape` and
`agentydragon/gaffer-private` are Terraform-managed alongside those adopted Forgejo
repos in `tf/gitops/forgejo-agentydragon-repos`.
