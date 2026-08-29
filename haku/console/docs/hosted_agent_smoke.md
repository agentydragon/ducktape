# Hosted-agent smoke runbook

Paste-ready prompts that walk a hosted agent through verifying its own plumbing: identity,
grant introspection, the egress fence (positive with credential substitution, negative, and the
substituted GitHub identity), the full temporary-grant lifecycle for **both** HTTP egress and
Kubernetes, the standing Kubernetes gate, and (for Haku) memory search. One prompt per agent
kind; each is a **single message** and the agent completes every step in one turn, so the
runbook works even on a harness that cannot yet take a second turn.

How to run: open a fresh conversation with the agent, paste the matching prompt verbatim, then
watch the approvals drawer — each prompt queues **two** approval-gated calls (one HTTP
`create_grant`, one Kubernetes `create_grant`); approve them when they appear. Read the final
PASS/FAIL table. A clean run takes about five minutes including the two approvals.

Operator notes on expected behavior:

- The fence-negative step _should_ fail with `CONNECT tunnel failed, response 403`, while exposing
  `X-Haku-Egress-Denied`, `X-Haku-Grant-Scope`, and `X-Haku-Egress-Help` headers. PASS means the
  agent reports the denial reason, reads the grant scope/how-to, and correctly explains what it
  would request; a refusal without that context is #5181.
- The GitHub-identity step is the substitution's real payoff: it proves the fence swapped the
  inert placeholder for the **real** bot token AND that the token is the intended identity. PASS
  = the API reports `agentydragon-agent`. Anything else (the placeholder echoed back, a 401, a
  different login) is a substitution or credential-wiring finding.
- Both grant-lifecycle steps end by revoking the grant and re-testing; the re-test failing again
  is the PASS. The Kubernetes one checks authorization with `kubernetes_can_i` (the oracle,
  independent of any kubectl wiring) and then, if the agent has its own kubectl against the
  console proxy, exercises that too — a missing kubectl there is a reportable BLOCKED, not a
  failure, and is itself signal about the own-kubectl path (#5182).
- If the agent stalls waiting for an approval, that is a finding, not a crash: the prompt tells
  it to poll and to report `pending_approval` honestly.
- Steps name tools by their catalog names (`grants__whoami` style); the exact ids the harness
  shows may carry a server prefix. An agent that cannot find a tool should say so rather than
  improvise — that too is signal.

## Prompt A — Haku (claude harness)

```text
Smoke-check your plumbing. Work through every step in this one turn, in order; do not stop at
the first failure. For each step print one line: "N. PASS/FAIL/BLOCKED — <one-line evidence>".
Never print token or credential values. Finish with a table of all steps and a short list of
anything that surprised you.

1. Identity: call the grants server's whoami. Report your agent name, agent id, and session id.
2. Grant introspection: list the grants that apply to you (principal: self). Report how many
   are active, how many are non-expiring standing grants, and the soonest finite expiry.
3. Fenced git (ref advertisement + pack transfer): first
   `git ls-remote https://git.allegedly.works/haku/haku-state.git | head -2` (PASS = refs
   print), then a shallow clone into a temp dir:
   `git clone --depth 1 https://git.allegedly.works/haku/haku-state.git /tmp/smoke-haku-state`
   — PASS = the clone succeeds and `/tmp/smoke-haku-state/AGENTS.md` exists. Remove the temp dir
   afterward. This exercises the egress fence, the proxy-auth challenge, Forgejo credential
   substitution, and the full fetch/pack path.
4. GitHub substituted identity: run
   `curl -sS --max-time 8 -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user`
   and report only the `login` field. The fence substitutes your inert placeholder for the real
   bot token on this origin. PASS = login is `agentydragon-agent`. Never print the token value.
5. Fence negative: run `curl -sS -D - --max-time 8 https://example.com/`. Expected: a
   "CONNECT tunnel failed, response 403" denial with the `X-Haku-Egress-Denied`,
   `X-Haku-Grant-Scope`, and `X-Haku-Egress-Help` response headers. PASS = you report the denial
   reason and grant scope/how-to, then explain in one sentence what you would do if a task needed
   that origin.
6. HTTP temporary-grant lifecycle: request a grant with the grants server's create_grant —
   grants [{domain: http, spec: {origin: {scheme: https, host: docs.anthropic.com, port: 443},
   coverage: {methods: [GET, HEAD]}}}], duration_seconds 1800, principal {kind: session,
   session_id: <your session id from step 1>}, rationale "smoke runbook step 6",
   wait_for_result_ms 60000. If you get a pending_approval stub, poll get_tool_call until it
   resolves. Once active:
   a. `curl -sS -o /dev/null -w '%{http_code}\n' --max-time 8 https://docs.anthropic.com/`
      — PASS = 2xx or 3xx.
   b. Revoke that grant with revoke_grants (its grant id, reason "smoke done").
   c. Re-run the curl — PASS = denied again (403).
7. Kubernetes standing gate: call kubernetes_can_i for one read you expect to have (for example
   list pods in your own sandbox namespace) and one you expect not to have (get secrets in
   kube-system). Report both answers. PASS = they match your expectations.
8. Kubernetes temporary-grant lifecycle: pick a read you just confirmed you LACK — list pods in
   the `haku-console` namespace is a good, non-secret target.
   a. kubernetes_can_i that exact read — expect "no".
   b. create_grant — grants [{domain: kubernetes, spec: {scope: {kind: namespaces, namespaces:
      ["haku-console"]}, rules: [{api_groups: [""], resources: ["pods"], verbs: ["get","list"]}]}}],
      duration_seconds 1800, principal {kind: session, session_id: <your session id>},
      rationale "smoke runbook step 8", wait_for_result_ms 60000. Poll if you get a stub.
   c. kubernetes_can_i the same read again — PASS = now "yes".
   d. If you have your own kubectl configured against the console's Kubernetes proxy, run
      `kubectl get pods -n haku-console` and report whether it succeeded; if you have no such
      kubectl, mark this sub-step BLOCKED with what you looked for.
   e. Revoke that grant with revoke_grants.
   f. kubernetes_can_i once more — PASS = "no" again.
9. Memory: search your index for "smoke runbook". Report the top hit, or state cleanly that
   nothing matched and whether the index reported itself behind.
10. Session introspection: list your sessions (limit 1) via haku_conversations and confirm your
    current session appears.

Rules: least noise — no retries in a loop, no widening any request beyond what a step names.
If a step's tool is missing from your list, mark it BLOCKED with the tool name you looked for.
```

## Prompt B — public-coder (codex harness)

```text
Smoke-check your plumbing. Work through every step in this one turn, in order; do not stop at
the first failure. For each step print one line: "N. PASS/FAIL/BLOCKED — <one-line evidence>".
Never print token or credential values. Finish with a table of all steps and a short list of
anything that surprised you.

1. Identity: call the grants server's whoami. Report your agent name, agent id, and session id.
2. Grant introspection: list the grants that apply to you (principal: self). Report how many
   are active, how many are non-expiring standing grants, and the soonest finite expiry.
3. Fenced git (ref advertisement + pack transfer): first
   `git ls-remote https://github.com/agentydragon/ducktape.git | head -2` (PASS = refs print),
   then a shallow single-branch clone into a temp dir:
   `git clone --depth 1 --single-branch https://github.com/agentydragon/ducktape.git /tmp/smoke-ducktape`
   — PASS = the clone succeeds and `/tmp/smoke-ducktape/README.md` exists. Remove the temp dir
   afterward. This exercises the egress fence, the proxy-auth challenge, GitHub credential
   substitution, and the full fetch/pack path.
4. GitHub substituted identity: run
   `curl -sS --max-time 8 -H "Authorization: Bearer $GITHUB_TOKEN" https://api.github.com/user`
   and report only the `login` field. The fence substitutes your inert placeholder for the real
   bot token on this origin. PASS = login is `agentydragon-agent`. Never print the token value.
5. Fence negative: run `curl -sS -D - --max-time 8 https://example.com/`. Expected: a
   "CONNECT tunnel failed, response 403" denial with the `X-Haku-Egress-Denied`,
   `X-Haku-Grant-Scope`, and `X-Haku-Egress-Help` response headers. PASS = you report the denial
   reason and grant scope/how-to, then explain in one sentence what you would do if a task needed
   that origin.
6. HTTP temporary-grant lifecycle: request a grant with the grants server's create_grant —
   grants [{domain: http, spec: {origin: {scheme: https, host: docs.github.com, port: 443},
   coverage: {methods: [GET, HEAD]}}}], duration_seconds 1800, principal {kind: session,
   session_id: <your session id from step 1>}, rationale "smoke runbook step 6",
   wait_for_result_ms 60000. If you get a pending_approval stub, poll get_tool_call until it
   resolves. Once active:
   a. `curl -sS -o /dev/null -w '%{http_code}\n' --max-time 8 https://docs.github.com/`
      — PASS = 2xx or 3xx.
   b. Revoke that grant with revoke_grants (its grant id, reason "smoke done").
   c. Re-run the curl — PASS = denied again (403).
7. Kubernetes standing gate: call kubernetes_can_i for one read in your own sandbox namespace
   and one outside it (for example get secrets in kube-system). Report both answers honestly —
   a profile with no Kubernetes standing access reporting "no" to both is a PASS, not a failure.
8. Kubernetes temporary-grant lifecycle: pick a read you just confirmed you LACK — list pods in
   the `haku-console` namespace is a good, non-secret target.
   a. kubernetes_can_i that exact read — expect "no".
   b. create_grant — grants [{domain: kubernetes, spec: {scope: {kind: namespaces, namespaces:
      ["haku-console"]}, rules: [{api_groups: [""], resources: ["pods"], verbs: ["get","list"]}]}}],
      duration_seconds 1800, principal {kind: session, session_id: <your session id>},
      rationale "smoke runbook step 8", wait_for_result_ms 60000. Poll if you get a stub.
   c. kubernetes_can_i the same read again — PASS = now "yes".
   d. If you have your own kubectl configured against the console's Kubernetes proxy, run
      `kubectl get pods -n haku-console` and report whether it succeeded; if you have no such
      kubectl, mark this sub-step BLOCKED with what you looked for.
   e. Revoke that grant with revoke_grants.
   f. kubernetes_can_i once more — PASS = "no" again.

Rules: least noise — no retries in a loop, no widening any request beyond what a step names.
If a step's tool is missing from your list, mark it BLOCKED with the tool name you looked for.
```
