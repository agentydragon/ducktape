# Hosted-agent smoke runbook

Paste-ready prompts that walk a hosted agent through verifying its own plumbing: identity,
grant introspection, the egress fence (positive, negative, and substitution), the full
temporary-grant lifecycle, the Kubernetes gate, and (for Haku) memory search. One prompt per
agent kind; each is a **single message** and the agent completes every step in one turn, so the
runbook works even on a harness that cannot yet take a second turn.

How to run: open a fresh conversation with the agent, paste the matching prompt verbatim, then
watch the approvals drawer — each prompt queues **exactly one** approval-gated call (the
`create_grant` in the grant-lifecycle step); approve it when it appears. Read the final
PASS/FAIL table. A clean run takes about five minutes including the one approval.

Operator notes on expected behavior:

- The fence-negative step _should_ fail with a bare `CONNECT tunnel failed, response 403` — the
  agent reporting that mute denial and correctly interpreting it is the PASS. Refusals that
  explain themselves are #5181.
- The grant-lifecycle step ends by revoking its own grant and re-testing; the re-test failing
  again is the PASS.
- If the agent stalls waiting for the approval, that is a finding, not a crash: the prompt tells
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
   are active and the soonest expiry.
3. Fenced git + credential substitution: run
   `git ls-remote https://git.allegedly.works/haku/haku-state.git | head -2`
   from your workspace. PASS = refs print. This exercises the egress fence, the proxy-auth
   challenge, and Forgejo credential substitution in one go.
4. Fence negative: run `curl -sS --max-time 8 https://example.com/`. Expected: a bare
   "CONNECT tunnel failed, response 403". PASS = you got that denial AND you explain in one
   sentence what it means and what you would do if a task actually needed that origin.
5. Temporary-grant lifecycle: request a grant with the grants server's create_grant —
   origin {scheme: https, host: docs.anthropic.com, port: 443}, coverage methods [GET, HEAD],
   duration_seconds 1800, principal {kind: session, session_id: <your session id from step 1>},
   rationale "smoke runbook step 5", wait_for_result_ms 60000. If you get a pending_approval
   stub, poll get_tool_call until it resolves. Once active:
   a. `curl -sS -o /dev/null -w '%{http_code}\n' --max-time 8 https://docs.anthropic.com/`
      — PASS = 2xx or 3xx.
   b. Revoke that grant with revoke_grants (its grant id, reason "smoke done").
   c. Re-run the curl — PASS = denied again (403).
6. Kubernetes gate: call kubernetes_can_i for one read you expect to have and one you expect
   not to have (for example: list pods in your sandbox namespace; get secrets in kube-system).
   Report both answers. PASS = the answers match your expectations and you say what you would
   do if a task needed the missing one.
7. Memory: search your index for "smoke runbook". Report the top hit, or state cleanly that
   nothing matched and whether the index reported itself behind.
8. Session introspection: list your sessions (limit 1) via haku_conversations and confirm your
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
   are active and the soonest expiry.
3. Fenced git + credential substitution: run
   `git ls-remote https://github.com/agentydragon/ducktape.git | head -2`.
   PASS = refs print. This exercises the egress fence, the proxy-auth challenge, and GitHub
   credential substitution in one go.
4. Fence negative: run `curl -sS --max-time 8 https://example.com/`. Expected: a bare
   "CONNECT tunnel failed, response 403". PASS = you got that denial AND you explain in one
   sentence what it means and what you would do if a task actually needed that origin.
5. Temporary-grant lifecycle: request a grant with the grants server's create_grant —
   origin {scheme: https, host: docs.github.com, port: 443}, coverage methods [GET, HEAD],
   duration_seconds 1800, principal {kind: session, session_id: <your session id from step 1>},
   rationale "smoke runbook step 5", wait_for_result_ms 60000. If you get a pending_approval
   stub, poll get_tool_call until it resolves. Once active:
   a. `curl -sS -o /dev/null -w '%{http_code}\n' --max-time 8 https://docs.github.com/`
      — PASS = 2xx or 3xx.
   b. Revoke that grant with revoke_grants (its grant id, reason "smoke done").
   c. Re-run the curl — PASS = denied again (403).
6. Kubernetes gate: call kubernetes_can_i for one read in your own sandbox namespace and one
   outside it (for example: get secrets in kube-system). Report both answers honestly — a
   profile with no Kubernetes standing access reporting "no" to both is a PASS, not a failure.

Rules: least noise — no retries in a loop, no widening any request beyond what a step names.
If a step's tool is missing from your list, mark it BLOCKED with the tool name you looked for.
```
