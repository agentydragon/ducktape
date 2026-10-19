# Runtime B operations (control plane + debugging)

Read when reactivating, re-provisioning, or debugging a session. Beta-surface field/flag names
(`agent_toolset_20260401`, `vault_ids`, the deployment schema) follow the `ant` docs as of 2026-06
— verify with `ant <cmd> --help`.

Recorded control-plane IDs: agent `agent_01CV5VupX8ALuVD1dsoEzHY6`, deployment
`depl_011DSrUoXuhoDWJoPyDuePqR` (haku-scan), environment `env_015uqL9WAMSDytQEWWmLG9zF`. The agent
is inactive until a fresh vault is assigned to the deployment.

## Bring-up (only after explicit reactivation)

```sh
# Fresh vault + MCP credentials first; never use the ID in the stale haku-cloud-agent-ids Secret.
HAKU_MANAGED_AGENT_VAULT_ID="$VAULT_ID" ./provision.sh  # org API key, outside the worker
# generate the environment key in the Console -> ANTHROPIC_ENVIRONMENT_KEY secret
# deploy haku-managed-agent (env key + the HAKU_* clone/git env) in haku-sandbox
ant beta:deployments run --deployment-id "$DEPL_ID"   # test one run, watch in Console
```

## Updating agent / deployment

`haku.{agent,environment,deployment}.yaml` are applied with `ant` (org `ANTHROPIC_API_KEY`), not
Flux; editing the YAML changes nothing live. After editing `haku.agent.yaml`:

```sh
# 1. Push a new agent version. YAML is the request body; --version is the CURRENT version.
cur=$(ant beta:agents retrieve --agent-id "$AGENT_ID" --transform version -r)
ant beta:agents update --agent-id "$AGENT_ID" --version "$cur" < haku.agent.yaml

# 2. Re-pin: the deployment pins a specific agent version. A bare agent ID re-pins to latest.
ant beta:deployments update --deployment-id "$DEPL_ID" --agent "$AGENT_ID"
ant beta:deployments retrieve --deployment-id "$DEPL_ID" --transform '{agent}'  # confirm
```

`haku.deployment.yaml` (schedule, initial events) is applied with `ant beta:deployments update`
(`--schedule`, `--initial-event`).

**Gotcha:** a run uses the version pinned at run time, and a parked session keeps its creation
version — re-pin before triggering a wake and start a fresh session.

## Test and observe

```sh
ant beta:deployments run --deployment-id "$DEPL_ID" --transform '{id}'  # session_id in response
ant beta:sessions:events list "$SID" --format jsonl > events.jsonl      # default output is not a JSON array
ant beta:sessions delete --session-id "$SID"  # ends a stuck session; worker stops the work item, keeps polling

jq -r 'select(.type=="user.message" or .type=="agent.message")
       | "[\(.type)] \([.content[]?|.text//empty]|join(" "))"' events.jsonl
jq -r 'select(.type|test("tool_use$")) | "TOOL \(.name)  \(.input|tojson[:160])"' events.jsonl
jq -r 'select(.type|test("tool_result$")) | "\(.tool_use_id) is_error=\(.is_error)"' events.jsonl
```

Event types: `user.message` (wake), `agent.thinking`, `agent.message`, `agent.tool_use` /
`agent.mcp_tool_use`, `user.tool_result` (keyed by `tool_use_id`; `is_error=true` is a tool
failure, not a worker bug), `session.status_idle` / `session.thread_status_idle` with
`stop_reason`.

**Stuck session:** the `*_tool_use` with no matching `user.tool_result.tool_use_id` is pending. Idle
with `stop_reason.type == "requires_action"` means it awaits approval (`permission_policy:
always_ask`; set `always_allow` in `haku.agent.yaml` for unattended runs). The fixed empty-output
deadlock looked different: the result never posted because the worker 400'd; here results post
(`200 OK` in pod logs).

Worker side: `kubectl logs deploy/haku-managed-agent -n haku-sandbox` — `executing tool tool=…
tool_use_id=…`, then `POST …/events 200` per result, `work/…/heartbeat` holding the lease,
`session terminated` → `work/…/stop` at session end.
