# haku-ui on agentplane-staging, as its own caller

haku-ui's backend reads the operator's location (Home Assistant) and the Grocy shopping list
through haku-console, and both servers left the console on 2026-09-23 (#7671, #7673). Location
refreshes fail with `Unknown tool: 'home_assistant__ha_get_state'` and the backend serves the last
known fix. The shopping-list reader calls `grocy_sf__shopping_list_get`, which the console no longer
lists either. The `<tool-call>` buttons can only submit console-fronted tools.

**Target:** haku-ui reaches those through agentplane-staging as its own ServiceAccount,
`haku-sandbox/haku-ui`, and holds no credential. Its pod gets the egress sidecar that sandboxes
have, so a placeholder stands in for every credential:

- location and zones are GETs on Home Assistant's REST API (`/api/states/person.rai`,
  `/api/states`) through the `home-assistant-readonly` egress route;
- the shopping list is a GET on Grocy's REST API through the `grocy-sf-readonly` egress route;
- `<tool-call>` buttons become Action requests, which wait for the operator like any other write.

**Why not `claude-ai`:** that binding auto-approves sandbox create and exec, so anything acting as
`claude-ai` runs arbitrary code holding all of its egress credentials. haku-ui needs three reads.
Its requests would also be indistinguishable from Claude sessions in the request log and the
approval queue.

**Invariant: haku-ui's grants stay a subset of `claude-ai`'s.** `haku-sandbox-admin` lets Haku
create Pods that run as any ServiceAccount in `haku-sandbox`, and exec into haku-ui's, so
whatever is bound to `haku-ui` is Haku's too. Haku already holds `claude-ai`'s grants, so the
subset rule makes that harmless. The binding itself lives in `agentplane-staging`, which Haku
cannot write, and Haku cannot create or label ServiceAccounts in `haku-sandbox`.

## Where the Pod runs

haku-ui stays in `haku-sandbox`, where haku-state's Flux (`haku-state-workloads`) deploys it,
and agentplane admits callers from there.

- The egress proxy already separates the two: `allowed_service_account_namespaces` only
  authenticates, and its policies and bindings live in one namespace. Adding `haku-sandbox` is
  config.
- The Action Service does not. Its `allowed_service_account_namespaces` also names the
  namespaces whose policy sets, bindings and caller ServiceAccounts it watches. Split it the way
  the egress proxy is split: caller ServiceAccounts are watched in every allowed namespace, and
  policies and bindings only in `agentplane-staging`.

Moving haku-ui into `agentplane-staging` instead would need no agentplane change. But ducktape
would then have to own the Deployment: Haku-authored manifests applied there could run a Pod as
`claude-ai`.

## Steps

1. **Action Service:** split caller namespaces from the policy namespace, as above.
2. **ducktape** (`cluster/cdk8s/agentplane/`):
   - the `haku-sandbox/haku-ui` ServiceAccount, carrying `agentplane.allegedly.works/use-action-service`;
   - an egress binding for it: `home-assistant-readonly`, `grocy-sf-readonly`, and the Action
     Service rule on its own — `basic` carries it but also LLM access, which haku-ui does not
     need;
   - `haku-sandbox` in both services' allowed namespaces;
   - network policy: haku-ui to `agentplane-egress:8888`, and the proxy admitting it, past
     `haku-sandbox-force-proxy-egress`;
   - the proxy's CA bundle available in `haku-sandbox`.
3. **haku-state** (a Forgejo PR, since `ui/` and `k8s/` are code trees):
   - the StatefulSet runs as `haku-ui` and gets the egress sidecar, the projected token volume,
     the proxy env and the CA mount; the sidecar image gets an image policy next to haku-ui's own;
   - `ui/backend/console_mcp.py`'s location and shopping-list readers move to the Home Assistant
     and Grocy REST routes; tool-call submission moves to Action requests.
4. **Verify live:** a location refresh returns a fix through the egress proxy as
   `haku-sandbox/haku-ui`, and the Kitchen tab's shopping list renders.
