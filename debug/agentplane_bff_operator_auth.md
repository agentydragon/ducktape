# Agentplane operator federation deployment gate

PR [#5820](https://github.com/agentydragon/ducktape/pull/5820) implements the browser-to-Action
operator boundary. Runtime configuration, credential lifecycle, failure codes, and test targets
are documented in [operator federation](../x/agentplane/docs/operator_federation.md).

## Provider evidence and remaining gate

The existing [hostexec exchanger](../haku/console/tools/hostexec_token.py) supplies the acting
operator's Authentik access token as a JWT-bearer assertion to a target client. It does not turn
workload credentials into operator authority. [Authentik federation notes](../cluster/docs/mcp_oauth_authentik_notes.md)
and the hostexec implementation are the reuse basis, not a new signing authority.

The inspected [login provider](../tf/gitops/sso-providers/provider_agentplane_staging.tf) uses a
per-provider issuer and hashed subjects. The target provider now uses the same subject mode, so
federation preserves the Authentik user's `uid` without a local subject mapping. Its application
policy is the authorization boundary; the Action Service verifies only the target issuer, audience,
signature, and lifetime.

**Deployment remains blocked:** checked-in staging configuration now supplies the Action federation
target as a Git-owned non-secret ConfigMap, while Terraform owns the Authentik provider policy and
credential-bearing Secrets. Its
[app network policy](../cluster/k8s/agentplane-staging/app/networkpolicy.yaml) also needs review for
the Action/JWKS routes before enablement. Validate subject continuity and a denied operator against
the real provider before enabling. Signed mock evidence is not live Authentik policy proof.

No live cluster edits, real token exchanges, or production secret creation are part of this PR.
The runner bridge remains single-replica; PostgreSQL session sharing does not make its in-memory
attachments safe to scale.

## Implementation validation

- [Remote regression and typecheck run](https://app.buildbuddy.io/invocation/68b35f69-3d85-40f7-b5b7-dcf18c088f2c):
  all 16 test targets passed (10 executed, 6 cached). Includes the complete Action Service test
  package, app action/auth/main/API/trajectory tests, frontend Vitest and Action visual render.
  All changed Python libraries plus frontend Action modules and bundle were explicitly named;
  default Ruff/mypy/ESLint aspects and TypeScript compilation were enabled.
- [Remote hooks and Gazelle convergence](https://app.buildbuddy.io/invocation/d8486473-014d-42df-9968-db411d0d83e4):
  all applicable hooks passed; no hook exclusions. Hooks ran on the BuildBuddy runner, not in the
  agent Pod. The opt-in cached-test enforcement hook was not enabled; test evidence is the run above.
- Negative-login testing exposed acceptance of a wrong-audience ID token by the inherited Authlib
  path. The callback now explicitly checks audience/authorized party; the signed rejection test
  passes alongside issuer/signature/expiry/state/nonce and cross-replica callback/logout coverage.

These runs used the recovered worktree patch over `eea5f5cb5`, not a deployed provider. No local
Bazel/Bazelisk/pytest or live cluster changes were used.
