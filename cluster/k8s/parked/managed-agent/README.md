# haku-managed-agent — Managed Agents self-hosted worker (Runtime B)

In-cluster worker for Haku on Anthropic Managed Agents. It long-polls
Anthropic's work queue (`ant beta:worker poll`) and executes tool calls inside
`haku-sandbox`. Design, trust split, and the control-plane provisioning live with
the component: <../../../../haku/runtime/managed_agent/self_hosted/README.md>.

The image is the full-NixOS `.#haku-managed-agent-image`, built and pushed to
`git.allegedly.works/ducktape-ci/haku-managed-agent` by
`.github/workflows/haku-managed-agent-image.yml`; Flux tracks the tag via the
`haku-managed-agent` ImagePolicy. We don't boot the image (no systemd): the pod
runs the worker closure directly as non-root `haku` — see `deployment.yaml` and
`../../../../haku/runtime/managed_agent/self_hosted/nixos.nix`.

## Manual prerequisite — NOT yet turnkey

This is required for the worker to see fresh `haku/base` / `haku/run.md` content
and is **not** provisioned declaratively yet:

- **Forgejo ducktape mirror** (`git.allegedly.works/agentydragon/ducktape`): the
  worker clones ducktape from this in-cluster mirror, not GitHub (the egress
  proxy blocks GitHub). The mirror is **bumped manually** — it is not
  auto-synced from GitHub. Push the mirror before expecting Haku to see new
  `haku/base` / `haku/run.md` content.

The `haku` Forgejo user's read grants on `agentydragon/ducktape` and
`agentydragon/gaffer-private` are Terraform-managed alongside those adopted
Forgejo repos in `tf/gitops/forgejo-agentydragon-repos`.

## Bring-up runbook (if ever recreated from scratch)

Live and active today (`suspend: false`); this is the from-zero sequence, not a
pending step:

1. **Generate the environment key** in the Console (Environments →
   `haku-selfhosted` → _Generate environment key_). It is an `sk-ant-oat01-…`
   value, created only via the Console — never the API.
2. **Set the secret**: `sops cluster/k8s/haku/managed-agent/environment-key.sops.yaml`
   and replace the placeholder `environment_key` with the generated value.
3. **Confirm the env wiring** in `deployment.yaml`: `ANTHROPIC_ENVIRONMENT_ID`,
   `HAKU_GIT_HOST` (the in-cluster Forgejo Service `forgejo-http.forgejo`), and
   `HAKU_DUCKTAPE_REPO_URL` (the in-cluster ducktape mirror). Both git clones are
   authed by the `haku` `.netrc` line for that host.
4. **Smoke test**: `ant beta:deployments run --deployment-id depl_011DSrUoXuhoDWJoPyDuePqR`
   (org `ANTHROPIC_API_KEY`, off the worker) and watch the session in the Console.
