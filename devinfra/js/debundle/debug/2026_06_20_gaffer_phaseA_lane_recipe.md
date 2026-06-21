# gaffer-private P4 Phase A selector-stabilization — lane recipe (battle-tested)

Self-contained instructions for a `debundle_lane_worker` converting one tana/re
spec family's fragile minified-name pins to genuine stable `source_match`
selectors. Proven across the billing / infra / app-commands lanes (2026-06-20).
Read this + the two skills, then execute the rolling workflow.

Skills: <../skills/debundle_lane_worker/SKILL.md>, <../skills/debundle_stabilize/SKILL.md>.

## Mission

Replace `source_match: { binding: { name: <minified token> } }` pins with selectors
anchored on **genuine forward-stable identity** — minimized, not over-bound — keeping
the debundle generated output **byte-identical** (`regen_js_test` green).

## ⚠️ Durability: push incrementally

The web container can be reclaimed during idle and will kill a running lane — only
**pushed** commits survive. Push the lane branch after the first 1–2 conversions, then
commit+push after every ~5. Never accumulate unpushed work.

## Environment recipe (degraded web session — exact forms; deviating breaks RBE)

All bazel commands need `dangerouslyDisableSandbox: true`. Load the key per shell:
`source /home/user/ducktape/devinfra/secrets/web_env.sh` (sets `BUILDBUDDY_API_KEY`;
`SOPS_AGE_KEY` is in the session env).

**Queries** via the `debundle_cli` wrapper (sets `DEBUNDLE_MODULES`/`SOURCE_ROOT`/`GRAPH`).
**No `--platforms=`** (strips RBE container identity ⇒ `PERMISSION_DENIED: Container
identity unknown`); **all bazel flags before `--`**:

```
bazelisk \
  --host_jvm_args=-Djavax.net.ssl.trustStore=/etc/ssl/certs/java/cacerts \
  --host_jvm_args=-Djavax.net.ssl.trustStorePassword=changeit \
  run //tana/re:debundle_cli --config=nolint --config=rbe \
  --remote_header="x-buildbuddy-api-key=${BUILDBUDDY_API_KEY}" --shell_executable=/bin/bash \
  -- <subcommand>
```

**Byte-identical gate** (from the lane worktree): `/tmp/bz test
//tana/re/web/78d928dca7:regen_js_test --config=nolint` → must print `PASSED`. Run after
every batch. Never add `--platforms=` or `--output_base` to the gate.

**Transient flake:** `PERMISSION_DENIED: Container identity unknown` appears
intermittently on pipeline-rebuilding commands; the identical invocation succeeds on
retry — wrap such commands in a ×3 retry.

## Flag facts (verified)

- `spec selector-debt` has **no `--module-prefix`** — run whole-spec (`-- spec
selector-debt --group-module-depth 2 --format json`) and filter to your family
  (`name_only[].module`, or `name_only_module_groups[].module_prefix`/`name_only_count`).
- `spec synthesize-selectors` / `match-selector` **do** take `--module-prefix <fam>`. For
  name→source_match use `--rewrite name-binding-to-source-match`. They need the source
  chunk explicitly: `--source-file <abs>/static/index-DI2GynTv.js` (the main chunk; every
  family lives in it) — `DEBUNDLE_SOURCE_ROOT` alone is not honored by `--chunk`.
- The minimizer frequently **skips** items ("no sparse selector"/malformed) — expect to
  hand-author most selectors and prove each with `match-selector`.

## Matcher gotchas (verified)

- Declaration **keyword must match** the source (`var`/`const`/`let`).
- `ANYTHING` is **invalid in a binding-identifier / assignment-target slot** — use a real
  alpha-renamable name there; `ANYTHING` is for expression/value/statement slots.
- `else`-with-single-statement uses `STMT`, not `STMT_LIST`.
- `match-selector` **over-reports** uniqueness: a bare holed `function X(ANYTHING){STMT_LIST}`
  matches 1000+ nodes because the name alpha-renames. Uniqueness must come from a kept
  **literal/signature**. The authoritative arbiter is `regen_js_test`, not `match-selector`.

## Anchor playbook (strongest → weakest)

1. **Self-emitted literal** — error/log/event strings, thrown `Error` messages, URL/route
   prefixes, `Symbol.for("…")`, regex literals. Minifier-immune; the strongest tier.
2. **Rich destructured-param signature** — `{ onOpenCommandLine, onMove, onTagNode, … }`;
   distinctive prop/param names survive minification.
3. **Stable member/property fingerprint** — `.startSpan`/`.setAttribute`, distinctive
   option-bag keys, distinctive method names.
4. **Adjacent-class / sibling-declaration anchor** — for boilerplate with no self-identity
   (e.g. esbuild decorate-helper trios emitted ~32×): a `binding_group` +
   `DECLARATORS_AFTER` keyed off an adjacent named class.

Always hole the mechanism (`ANYTHING`/`STMT_LIST`/`DECLARATORS`/`ARGS`) so the anchor is
identity, not a body photograph. **Reject** (do not ship as if stable): hashed chunk URLs
(`import("./index-<hash>.js")`), registration-roster/long-body photographs,
neighbor-borrowed literals, and deeply-nested-only discriminators.

## Hard rules

- **Genuine stability only.** No faithful stable anchor today → **leave it a plain
  name-pin** and report the dead-end. Never fake an anchor.
- **Do NOT add `comment:` to debt pins** — member `comment:` EMITS to the JS output and
  breaks the byte-identical gate (unless you regen, which churns the snapshot). Leave debt
  pins un-commented; the `selector-debt` count tracks them; explain dead-ends in the report.
- Only edit spec YAML under your family's dir. NEVER edit `.../js/**` (pipeline output).

## Commit + push

`cd /home/user/ducktape && nix develop --command git -C <worktree> commit -F <msgfile>`
(prettier may reformat YAML → re-stage + recommit). Footers exactly:

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JYRJift7ufbrfsdXTZDyaS
```

Push the lane branch (`git -C <worktree> push -u origin <branch>`, retry ×4 exp backoff).
No PR. No model identifier in any committed artifact.

## Report (concise)

lane branch + pushed commit hashes; family selector-debt before → after; `regen_js_test`
result; # converted + 3–5 examples naming the anchor TYPE; pins left as debt + WHY
(dead-end / named tooling gap); any new command/flag workaround.
