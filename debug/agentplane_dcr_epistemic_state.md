# Epistemic state: Agentplane DCR and consent

Updated 2026-09-09. Research/probe only; no production mutations.

## Objective and action space

Find the smallest composition reusing pinned OAuth libraries while meeting integration-app consent,
Connection bookkeeping and immutable Action provenance. Direct levers: inspect source/tests, browse
official docs, hermetic Bazel probe, publish a scoped review. User-mediated levers: choose authority/
cardinality/rebind/receipt semantics and later deployment target. No new external access is needed
for this research. Stop when implementation can be split against reviewable contracts and remaining
product choices are explicit; do not implement a broad auth system from unresolved assumptions.

## Uncertainty register and hypotheses

| ID  | Question                                                         | Prior                               | Current evidence/state                                                                                                 |
| --- | ---------------------------------------------------------------- | ----------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| U1  | Public external-consent handoff sufficient for protocol routing? | Plausible; source inspection needed | Resolved for the tested path: hermetic probe passes                                                                    |
| U2  | Public APIs also cover principal/grant convergence?              | Unclear                             | Existing Haku needs private code store and protected claim hook; unresolved minimal adapter                            |
| U3  | Existing storage/federation reusable?                            | Likely                              | PostgreSQL KV, operator session and BFF verification implementations exist; new endpoint/auth integration still needed |
| U4  | Connection cardinality and receipt ownership?                    | Product choices                     | Many Connections/Identity and Identity-scoped receipts proposed, not approved                                          |
| U5  | External-consent setting enforces browser binding?               | Unknown                             | Resolved false: stock callback binding check is omitted in external mode                                               |

Competing initial architecture hypotheses [VIBE, qualitative engineering estimates]: H1 public
FastMCP hooks plus small authority adapter, 0.60; H2 extract Haku's larger private seam, 0.25;
H3 build on Authlib as a separate AS, 0.10; other, 0.05. These are prioritization estimates, not
measured probabilities. The distinguishing test is the public handoff/code-exchange probe plus a
source audit of principal extraction. No numerical posterior is claimed from code inspection.

## Evidence log

- Observation, baseline `946231a571`: requirements pin `fastmcp==3.4.4`,
  `fastmcp-slim[client, server]==3.4.4`, `authlib==1.7.2`; cache distribution metadata agrees.
  Resolves version ambiguity, U1/U2. Cost: local source reads.
- Observation, pinned `OAuthProxy.authorize`: `if self._require_authorization_consent in
(False, "external")`; stored transaction TTL `15 * 60`; returns the upstream URL.
  `_handle_idp_callback` checks browser cookie only for `(True, "remember")`. U1/U5: routing
  seam exists, consent authority does not. Cost: one targeted source read.
- Observation, `haku/console/identity/fastmcp_adapter.py`: `_SUPPORTED_FASTMCP_VERSION = "3.4.4"`;
  `exchange_authorization_code` reads `_code_store`; `_extract_upstream_claims` injects `grant_id`.
  U2: don't claim a public-only complete binding solution. Haku's application authority locks
  enrollment/grant transitions; its multi-operator Agent model is not Agentplane's contract.
- Observation, shared `persistence.py`: `PostgreSQLStore(url=u, table_name=t)`; integration app
  `FederatedOperatorActions.exchange` cryptographically checks source and target subjects.
  U3: reusable components exist, but no enrollment operator client exists yet.
- Research lookup, https://gofastmcp.com/servers/auth/oauth-proxy, accessed 2026-09-09:
  official overview supports the proxy/DCR composition. Installed pinned code determines exact
  hooks; current docs may describe a newer version. U1/U2 unchanged by generic documentation.
- Experiment authored: `//mcp_infra/authentik_auth:test_external_consent`, public override,
  real mock-OIDC HTTP flow, proxy replacement, PKCE and code replay checks. Result pending.
- Experiment, invocation `e51f2ec6-2199-40c6-a194-110ad1ccdfa8`: first run failed because
  the wrong-resource response was `error=server_error`, not `invalid_target`. Log evidence:
  `AuthorizationErrorResponse` rejects `input_value='invalid_target'`; FastMCP refused the
  resource before creating a consent interaction. Updated the probe to pin the observed refusal;
  no production adapter change. U1 remains open for the remaining callback/exchange flow.
- Observation, pinned `OAuthProxy.exchange_authorization_code`: `_code_store.delete` at line
  1113 precedes `_extract_upstream_claims` at line 1212, with an upstream-token store write between.
  U2: a protected claim hook could gate issuance, but cannot preserve pre-consumption retries.
  Haku's private preflight read therefore addresses a concrete ordering problem. Any extraction
  should preserve that checkpoint with a pinned test rather than replacing the OAuth protocol.
- Experiment, invocations `95e5d84f-33e8-48db-92ac-d1b4a03d3684` and
  `5e26fba7-c053-4da7-acd2-94806e58a6df`: replacement preserved the registration and callback,
  and wrong PKCE returned `401 {"error":"invalid_grant","error_description":"incorrect code_verifier"}`.
  Initial test assumed SDK HTTP 400; FastMCP's `auth.TokenHandler` intentionally transforms it
  to 401. Corrected the test to the actual wrapper contract. No production defect inferred.
- Observation, pinned `OAuthProxy.__init__`: `FernetEncryptionWrapper` is inside
  `if client_storage is None`; explicit storage is assigned directly as `_client_storage`.
  U3: PostgreSQL reuse does not imply the file fallback's encryption. Design must name the
  credential-bearing store/key policy rather than inherit an unsupported encryption claim.
- Experiment result, invocation `db73f3a5-33f3-40ed-9a65-2f53a0587676`: `1 test passes`,
  17.4 seconds on RBE. The public handoff, distinct interactions, replacement, validation,
  original callback/state, PKCE rejection, code replay rejection and restored downstream
  client identity all passed. U1 resolved for this path; U2/U4 remain explicitly open.
  Repository pre-commit hooks passed. No live-client acceptance claimed.

## Current state, action queue and stopping tree

Best current recommendation: AS and Connection/grant authority in Action Service, consent/management
UI and authenticated BFF in integration app. Reuse FastMCP/mcp_infra protocol/storage, not Haku's Agent
model. Residual counterexample: a public handoff can work while grant activation remains racy or an
unbound bearer passes; the probe does not rule this out. Browser/grant checks remain mandatory.

Next actions in descending information value: publish the scoped design/probe PR; review the
minimal principal/grant checkpoint before implementation; resolve the remaining product choices.
Estimates [VIBE]: broader implementation is many PRs and has lower immediate information value
until semantics are chosen.

If probe passes, publish scoped design/probe PR and dispatch independent slices after choices.
If a public seam fails, inspect exact failure before considering private hooks. If BuildBuddy is
unreachable outside sandbox, stop validation and report connectivity; no local protocol substitute.

## Vibes ledger

Architecture probability weights, relative effort and review-size estimates are judgments rather
than empirical rates. The decisive evidence is source and executable behavior, not those weights.
Cardinality/rebind/ownership are recommendations requiring operator choice; tests cannot choose them.
