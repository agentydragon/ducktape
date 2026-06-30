# haku-self-hosted-agent-tf

Flux `Terraform` CR that provisions Haku's **self-hosted** Managed Agent declaratively
via the `claude-managed-agents` provider — environment, agent, vault + MCP credentials
(tana-ro, gmail-labeling), and the scheduled wake deployment. TF root:
<../../../../tf/gitops/haku-self-hosted-agent/>.

Supersedes the retired imperative bring-up
(`haku/runtime/managed_agent/self_hosted/provision.sh` + `haku.{environment,agent,
deployment}.yaml`). The **worker** (`worker.py`, image + k8s in
[`../agent-worker/`](../agent-worker/)) is unchanged — it still polls the environment's
work queue and runs tool calls in-pod; only the control-plane definition moved to TF.

Sibling to [`../cloud-agent-tf/`](../cloud-agent-tf/) (the Anthropic-sandboxed variant).
Both are the same Haku in one spend-capped Anthropic workspace; they share the
`haku-cloud-anthropic-api-key` Secret (this kustomization `dependsOn` cloud-agent-tf for
it) but keep separate environments/agents/vaults because their tool postures differ.

## Cutover (recreate-fresh, one-time)

The TF `create` is **non-destructive**: it stands up a NEW environment/agent/vault/
deployment alongside the live imperative one, so the running worker keeps working until
you deliberately cut it over to the new environment.

1. **Apply** — merge; Flux reconciles the `Terraform` CR (`approvePlan: auto`). Confirm
   it's healthy and read the new IDs:
   `kubectl -n flux-system get secret haku-self-hosted-agent-ids -o yaml` →
   `environment_id` (env\_…), `deployment_id` (depl\_…).
   - If the first plan errors on the `self_hosted` env config or the deployment
     `schedule` block, fix `tf/gitops/haku-self-hosted-agent/main.tf` to the provider's
     actual schema (the error is non-destructive — all creates) and re-push.
2. **Generate the environment key** for the NEW env in the Anthropic Console
   (Environments → `haku-selfhosted` → _Generate environment key_). It is **never**
   created via the API.
3. **Point the worker at the new env** — in [`../agent-worker/`](../agent-worker/):
   put the new key in `environment-key.sops.yaml` (`environment_key`), and set
   `ANTHROPIC_ENVIRONMENT_ID` in `deployment.yaml` to the new `env_…` from step 1.
   Commit. The worker rolls (`strategy: Recreate`) onto the new environment.
4. **Verify** — `kubectl -n haku-sandbox logs deploy/haku-worker` shows it polling the
   new env id; trigger one run with `ant beta:deployments run --deployment-id <new depl>`.
5. **Retire the old imperative resources** — the recreate-fresh leaves the
   imperatively-created env/agent/vault/deployment orphaned (TF doesn't own them). Delete
   them once the worker is steady on the new env:
   `ant beta:deployments delete …`, `… agents delete …`, `… environments delete …`,
   `… vaults delete …` (the old IDs you recorded at original bring-up).

## Notes

- **gmail-labeling** is wired in from the start: the agent carries the `gmail-labeling`
  MCP + an `always_allow` toolset (safe — the server confines every op to `haku/` labels),
  and the vault holds its bearer (read in-cluster from `gmail-labeling/haku-gmail-labeling-token`).
- The doctrine that _grants_ this write lives in `haku/base/instructions.md`; the
  _policy_ for it in `haku/state_template/procedures/manage_gmail_labels.md` (seeded into
  `haku-state`).
