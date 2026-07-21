# Haku tooling delivery — constraints & open design space

How does Haku's run-tooling CLI (`haku read` / `advance` / `bookmark` / … — currently in
`haku-state/cli/`) become a **real command on PATH** in every runtime, without paying
bazel-at-runtime and without weakening the egress fence? This note records the **constraints**
(durable facts, worth not re-deriving) and the **forces** in the design; it deliberately does
**not** lock to a fixed option set — the space is open and the points explored so far (bottom)
are examples, not an enumeration.

Immediate symptom (F2 from the 2026-07-21 run-procedure walkthrough): every doc says
`haku read --all`, but there is no `haku` command — it's shorthand for a ~100-char
`PYTHONPATH="$(tools/agent_python.sh --pythonpath)" "$(tools/agent_python.sh)" -m cli.main …`
incantation, and no doc defines the alias. Today the CLI runs by **borrowing** the fastmcp
closure's interpreter (`tools/agent_python.sh` scrapes the `.#agent-haku` closure's `fastmcp`
wrapper for its interpreter + `site-packages`). That works, but is the "which python, ugh".

## Constraints (durable facts)

1. **Two independent egress fences, not one.**
   - **(a) Claude Code web-environment domain allowlist** (Anthropic-side, set at environment
     creation; the documented list is in [`../runtime/claude_web_env/README.md`](../runtime/claude_web_env/README.md)
     → _Allowed domains_ — `*.anthropic.com`, `*.allegedly.works`, `*.googleapis.com`,
     `*.buildbuddy.io`, `nixos.org`, `cache.nixos.org`). Fences the **web-home container** (Runtime
     A). Currently loosened to "any domain"; the operator intends to re-tighten. Mechanism:
     `network.allowedDomains` + the `anthropic-egress-control` proxy (see
     `devinfra/claude/web_env/docs/environment_discovery.md`).
   - **(b) Cluster mitmproxy L7 allowlist** on `haku-sandbox` **and `haku-ci`** (the
     agent-authored build compute) — see [`../docs/security.md`](../docs/security.md) enforcement
     inventory. Deliberately tight (Haku is assumed adversarial). This is the one the operator
     does **not** want to loosen casually.

2. **Bazel-at-runtime off-cluster is blocked structurally — not just by egress.**
   `haku-state/MODULE.bazel` resolves its ducktape dependency from an **in-cluster** service,
   `http://forgejo-http.forgejo:3000/haku/ducktape.git`, not a public FQDN. So `bazel run` in the
   web home fails even with github + `*.allegedly.works` fully open. **Bazel is a CI-only
   (in-cluster) tool here**; `//cli:{bookmark,validate,freshness}` targets exist for CI, not for
   the agent's interactive path.

3. **The Nix closure is the existing tool-delivery mechanism under the tight fence.**
   `fastmcp`/`tea`/`himalaya` reach PATH via `.#agent-haku`, installed by
   `claude_web_env/setup.sh` (web home) and the worker `entrypoint.sh` (self-hosted). It resolves
   as **Attic cache hits** (`cache.allegedly.works` + `cache.nixos.org`, both on the _tight_
   allowlist) — `flake.nix` is explicitly built so "restricted egress can substitute these
   fixed-output paths from Attic". The closure build + push runs on **ducktape's github
   `nix-attic-push` job** (`ubuntu-latest`, on push to `devel`). So any tool delivered through the
   closure needs **no** egress loosening.

4. **Exfil is contained by destination-trust, not the egress proxy** (see the sharpened statement
   now in [`../docs/security.md`](../docs/security.md) → _Doctrine_). The proxy stops **direct**
   exfil (pod → arbitrary host); it does **not** stop **laundered** exfil through an
   allowlisted write path (git push, CI publish, MCP write). Those are bounded by _where the write
   lands_ — private repo (safe), operator-reviewed ducktape (trusted), CSP-fenced browser
   (bounded). **Consequence for this decision:** a CLI delivery that has haku-state (where Haku
   writes freely) **publish a public artifact** would be a genuine _new_ laundered-exfil channel;
   ducktape-hosted-and-reviewed tooling is not (the operator gates every change).

5. **The CLI is not uniform in coupling.** The **source-reader** half (`read`/`sources`/`gmail`/
   `tana`/`cpap`/`console_audit`/`plaid`) needs only `bookmark_models` (the ledger schema). The
   **state-validator** half (`validate`/`freshness`) is coupled to `//model` — haku-state's
   item/board/ledger schema, which `ui/` _also_ consumes, i.e. **Haku's method** (self-evolved).
   So "move all of `cli/` somewhere" implicitly drags `model/` (and rewires `ui/`'s deps) with the
   validators; the reader is far more separable than the validators.

6. **`cli/` holds no personal data** — it's generic tooling; only the _state it reads/validates_
   is sensitive.

## Forces (what any answer trades off)

- **Self-management** — Haku evolving its own tooling (and its state schema) in `haku-state`,
  autonomously — vs **operator-reviewed delivery** (ducktape), which adds a trust gate but makes
  every change a PR.
- **One delivery path** — don't split the reader onto one mechanism and the validators onto
  another (confusing; rejected by the operator).
- **No new laundered-exfil channel** (constraint 4).
- **No bazel-at-runtime** (constraint 2) and, ideally, **no egress loosening** (constraint 1).
- **Ergonomics** — `haku` should be a bare, real command everywhere Haku runs (web home + every
  worker), not a memorized incantation.
- **Aesthetics / fragility of the closure-borrow scrape** — works today, but is indirect.

## Points explored so far (non-exhaustive — the space is open)

These are positions considered as of 2026-07-21, not a closed menu. Better shapes likely exist
(e.g. a first-class closure entry that references only a vendored/pinned reader; a hermetic
per-invocation env; something that reframes the reader as infra while keeping the schema in
state; …) — treat this as scaffolding to extend.

- **Thin `tools/haku` wrapper** (haku-state) + bootstrap symlink onto PATH. Ships now; fully
  self-managed; one path; **adds no channel** (local execution under the borrowed closure python);
  keeps the scrape hidden behind a clean command. Doesn't make the "which python" mechanism nicer,
  only invisible.
- **All of `cli/` (⇒ `model/`, ⇒ `ui/` rewiring) → public, reviewed ducktape**, delivered through
  the existing closure. Clean nix delivery, no scrape, and a review-gate that is a real _posture_
  upgrade. Costs: Haku loses autonomous evolution of its tooling **and state schema**; sizeable
  migration.
- **Publish `cli/` as a public artifact from haku-state CI**, built into the closure by ducktape's
  github job. **Discarded** — it's exactly the new laundered-exfil channel of constraint 4 (Haku
  writes `cli/` freely in haku-state; a public publish would carry whatever it injects).
- **Build the closure in-cluster** (haku-ci builds + pushes to Attic) so haku-state source never
  reaches a github runner. Keeps source self-managed; re-architects the Attic pipeline (currently
  github-only) and needs Nix in haku-ci — heavier.

## Adjacent open axes (documented elsewhere, noted so this decision stays coherent with them)

- **Which runtime** (A web-routine / B managed-agents / C self-hosted loop):
  [`runtime_options.md`](runtime_options.md). The closure path lands on PATH identically in A's
  web home and C's worker, so CLI-delivery is largely runtime-independent.
- **Subscription / ToS coverage** — see `runtime_options.md` → _The subscription paradox_ (now
  extended with the non-programmatic-use risk). Orthogonal to CLI delivery but part of the same
  "how does Haku run sustainably" question.

## Status

Undecided by design (operator wants the space kept open). F2 is currently _worked around_ by the
closure-borrow; the missing bare `haku` command is the live ergonomic gap. No egress fence needs
to move for any of the surviving directions.
