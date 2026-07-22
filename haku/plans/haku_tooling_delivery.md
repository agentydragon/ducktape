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

2. **Bazel-at-runtime off-cluster is blocked at two layers — and no fetch trick rescues it.**
   Empirically confirmed 2026-07-21 from the (any-domain-loosened) web-home container:
   - **(i) The ducktape module** resolves from an **in-cluster** service DNS name,
     `http://forgejo-http.forgejo:3000/haku/ducktape.git` — not a public FQDN, so it fails with
     `Could not resolve host` even with egress fully open. Repointing it at public github
     (`ducktape` _is_ a public repo, so no token needed) clears this layer.
   - **(ii) …then bazel's own ruleset deps fail.** Bazel fetches nearly every dependency as an
     `http_archive` from github **release / `/archive/` / `codeload`** URLs, and those paths are
     gated by **Claude Code's GitHub-App repository-access scope** — _not_ a CDN or egress block:
     the proxy returns `{"message":"GitHub access to this repository is not enabled for this
session. Use add_repo to request access."}` (the Anthropic `documentation_url` on it confirms the
     App layer, not GitHub). The gate keys on the **repo's** App scope, not the path type: the
     _same_ `codeload` URL returns 200 for an in-scope repo (`agentydragon/ducktape` → a 137 MB
     tarball) but 403 for an out-of-scope one. It is **not** confined to download surfaces either —
     a non-`add_repo`'d repo's plain **HTML page** (`github.com/<owner>/<repo>`) 403s too, and the
     bare root `https://github.com/` returns **400** `Request path could not be canonicalized` (the
     App-proxy can't map `/` to a repo). What stays un-gated for _any_ public repo is
     `raw.githubusercontent.com` and the **git protocol** — the latter only because git is rewritten
     via `insteadOf` onto a **separate** in-container git proxy that never runs the App-scope check,
     _not_ because `github.com`-over-HTTPS distinguishes git from tarballs. **The split still makes
     no coherent egress sense:** for an out-of-scope repo the App-mediated HTTPS paths (HTML,
     `/archive/`, `codeload`, `releases`) 403 while the git proxy and `raw` hand back that same
     repo's every byte (a shallow `git clone` of a 403'd repo returns the full tree) — an artifact
     of _where_ the App-scope check sits, not a deliberate "gate third-party deps" policy.
     **Independent confirmation that this is a distinct layer, not egress policy:** the container's
     own agent-egress proxy reports `"selective": false` at `$HTTPS_PROXY/__agentproxy/status`
     (any-domain mode — see `/root/.ccr/README.md`), so the generic egress proxy is _not_
     host-filtering; the `codeload` 403 therefore cannot originate there and must be the GitHub-App
     scope check. **Re-validated 2026-07-22 in a fresh session** — arbitrary non-GitHub hosts
     (`example.com`, `wikipedia.org`, `pypi.org`) all return 200, so egress is genuinely open and
     the 403 is not a stale artifact of a previously-tightened fence. (Caveat: `selective:false` is
     loosened-container state and would flip when the operator re-tightens fence (a) — but the
     App-scope layer that gates `codeload` is orthogonal to that and stays regardless.)
     It is `add_repo`-extensible, but bazel pulls **dozens** of transitive third-party dep repos
     (rules_python, rules_cc, bazel-skylib, apple_support, …), re-chased on every bump —
     impractical. (CA trust is a **non-issue** here: per `/root/.ccr/README.md`, bazel reads a
     managed block in `/etc/bazel.bazelrc`, not `JAVA_TOOL_OPTIONS` — the `WARNING: ignoring
     JAVA_TOOL_OPTIONS` is a red herring.)

   **The git protocol is the one open fetch path — but bazel won't use it without per-module
   overrides.** Reconfirmed 2026-07-22: `git ls-remote bazelbuild/bazel-skylib` (never
   `add_repo`'d) returns `HEAD`, while that same repo's `archive`/`codeload`/`releases/download`
   tarball URLs all 403. Bzlmod, though, resolves modules from the Bazel Central Registry, whose
   `source.json` points at exactly those tarball paths — so default resolution hits the 403s.
   Forcing bazel onto git means a **per-module `git_override` across the whole transitive closure**
   (each pinned to a hand-derived commit matching its BCR version, re-derived on every bump) — and
   genuinely the _whole_ closure: overrides are **root-only and don't recurse** (overriding a direct
   dep still lets _its_ deps resolve from BCR → 403), and with `--lockfile_mode=off` that transitive
   set isn't even enumerated in-repo to work from;
   there is **no global "fetch via git" switch** (`--experimental_downloader_config` only rewrites
   one http URL to another, it can't switch protocol). See the option in _Points explored_ — it's
   horrible but real.

   **And it doesn't matter which fetch path wins.** Every off-cluster route (`bazel vendor`, a
   shipped `--distdir`, `git_override`-the-closure, or tunnelling fetches through the cluster) still
   delivers bazel's **entire external dependency closure** just to run a small python CLI —
   strictly more overhead than shipping the python itself. **Bazel is the wrong tool for
   _running_ the CLI**; it's a CI build/test tool (works in-cluster). `//cli:{bookmark,validate,
freshness}` targets exist for CI. If bazel should _produce_ the shipped artifact, the shape is
   "**`bazel build` in CI → package the self-contained `py_binary` runfiles → ship via the
   cache**" — the same deliver-a-prebuilt-artifact family as the Nix closure, not bazel-at-agent-time.

   **Why remote cache/RBE doesn't rescue local `bazel run` either, and why the gate won't lift.**
   External-dep fetches run in **repository rules on the bazel client**, not as remotable actions —
   so a remote _cache_, and even RBE with a _local_ client, still fetch the closure into the gated
   container. The fetch only leaves the container by routing it through a remote that has egress —
   moving the whole bazel **client** off-box (BuildBuddy Remote Bazel, which `bbr` wraps) or
   delegating downloads via the Remote Asset API — i.e. the remote-runner/CI shape, not agent-runtime
   local `bazel run`. And the gate is deliberate, not a transient: the release-asset/App scope holds
   _regardless of the environment's network-access level_, and the ask to let CCR read public repos
   without an App install / `add_repo` was **closed as not planned** (anthropics/claude-code#57641).

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
- **`bazel build` the CLI in CI → ship the prebuilt `py_binary` runfiles via the cache** (see
  constraint 2). Same deliver-a-prebuilt-artifact family as the closure, but built by bazel
  in-cluster, so source stays self-managed in haku-state. Still needs an in-cluster build+push
  path, and the artifact bundles its own interpreter (heavier than the closure, which shares the
  one `fastmcp` python) — but it reuses the `//cli:*` targets that already exist for CI.
- **`git_override` the entire transitive closure onto the git protocol** (haku-state
  `MODULE.bazel`) so `bazel run` works off-cluster despite the codeload 403s. The git protocol is
  the only un-gated fetch path (constraint 2), so this is the _sole_ way to make bazel fetch its
  deps off-cluster without `add_repo`. **Horrible but real, documented for completeness:** a
  hand-maintained `git_override` per module (dozens — rules*python, rules_cc, bazel-skylib,
  platforms, protobuf, abseil, …), each pinned to a commit matching its BCR version and re-derived
  on every bump, forfeiting bzlmod's version resolution. Committed config beats per-session
  `add_repo` on recurrence, but it's fragile busywork — and it \_still* ships bazel's whole external
  closure to run a small CLI (constraint 2's "doesn't matter which fetch path wins"), so it buys
  nothing over shipping the python directly. Not recommended; recorded so the option isn't
  re-derived.

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
