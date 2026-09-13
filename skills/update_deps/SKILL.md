---
name: update_deps
description: >
  Automated dependency updates — reads Renovate dashboard, applies safe updates,
  produces tested PRs. One bulk PR for trivial bumps, separate PRs for
  non-trivial migrations. Use on a schedule or manually.
---

# Automated Dependency Updates

## Your Purpose

You maintain a **set** of dependency update PRs for this monorepo.

**Invariant**: the union of all open dep-update PRs, plus documented blockers,
covers ALL current Renovate dashboard items AND any other outdated deps you find.

Use probe PRs to get evidence quickly, then consolidate proven-safe updates into
larger review PRs. The review unit is a compatibility/risk boundary, not a
package name or programming language.

1. **Grouped safe-update PRs** (`deps/auto-update` or a descriptive
   `deps/batch-*` branch): several updates whose exact probe heads have passed
   required CI and only change versions, lockfiles, or generated metadata. This
   explicitly includes trivial Python package bumps: a `pyproject.toml`,
   requirements lock, or Bazel Python requirement version-only change belongs in
   a grouped PR when its resolver and affected CI checks are green.
2. **Coordinated compatibility batches**: related packages that must move
   together, such as a runtime plus its provider, plugin, or ABI peer. Keep these
   together even if they span ecosystems, and do not mix them into the safe batch
   until their joint compatibility is proven.
3. **Non-trivial migration PRs** (`deps/<package-slug>`): one update or one
   tightly related migration that changes source APIs, generated interfaces,
   runtime behavior, exact dependency constraints, or toolchain contracts.
   Risky major bumps stay here; a major version number alone is not sufficient
   reason to put an otherwise proven version-only update here.

You are **NOT done** until:

- `bazel test //...` passes on RBE for every open PR (verify via BuildBuddy
  invocation link)
- Every available update is either in a PR or has a documented blocker with
  specific evidence (see Evidence Requirements)

## PR Model

### Grouped safe-update PR

- Branch: `deps/auto-update`
- Title: `deps: bulk dependency updates (YYYY-MM-DD)`
- Contains: multiple individually proven version-only updates from compatible
  boundaries, including ordinary Python package bumps.
- Description records each source probe PR, exact tested head, old/new version,
  and the CI/BuildBuddy evidence used to admit it.
- Split into two or more grouped PRs only when lockfile ownership, generated
  output, compatibility coupling, or reviewer ownership makes the boundary
  meaningful. Do not split merely because dependencies use different languages.

### Non-trivial PRs

- Branch: `deps/<package-slug>` (e.g., `deps/reqwest-0.13`, `deps/rules-js-v3`)
- Title: `deps: migrate <package> to <version>`
- Contains: version bump + all required code changes for that migration
- Description: migration summary, API changes, changelog highlights

### Lifecycle

You manage the full set of PRs. On each run:

1. List all open dependency PRs and read their descriptions.
2. Reconcile them against the current Renovate dashboard.
3. Publish independent probe PRs promptly so CI can test them asynchronously.
4. As probes become green, continuously fold compatible ones into one or more
   grouped safe-update PRs. Start each group from current `devel`, cherry-pick
   or reapply the proven changes, run its full required CI, and link the source
   probe PRs. Close superseded probe PRs only after the grouped PR contains their
   changes and preserves their evidence.
5. Keep red, pending, conflicted, or semantically coupled updates out of the
   green group; rebase and shepherd them independently.
6. Open, close, force-push, or update PRs as needed.

## State Passing Between Runs

You are stateless. Your state lives in the PRs:

- **Grouped safe-update PR descriptions**: tables of what was updated, source
  probe evidence, what's blocked, and links to non-trivial PRs. Your future
  instance reads these first.
- **Non-trivial PR descriptions**: migration details, what was tried, what worked.
- **Commit history**: shows what changes were applied.

On every run, start by reading ALL existing dep-update PR descriptions to
understand what the previous run already tried and decided. Then diff against the
current Renovate dashboard to find what's new or changed.

## Branch Setup

```bash
# Fetch latest devel
git fetch origin devel

# List all existing dep-update PRs
gh pr list --search "deps:" --state open --json number,url,headRefName,title

# Check out bulk branch (create or rebase)
git fetch origin deps/auto-update 2>/dev/null && \
  git checkout deps/auto-update && git rebase origin/devel || \
  git checkout -b deps/auto-update origin/devel
```

## Gather Available Updates

### From Renovate dashboard

```bash
gh issue list --repo agentydragon/ducktape --search "Dependency Dashboard" \
  --json number -q '.[0].number' \
  | xargs -I{} gh issue view {} --repo agentydragon/ducktape --json body -q '.body'
```

Parse the "Pending Approval" section for available updates.

### Beyond Renovate

Also check for updates Renovate doesn't track:

- `tf.download(mirror = {...})` provider version pins in `MODULE.bazel`
- `tfdoc_version`, `tflint_version`, OpenTofu `version` in `MODULE.bazel`
- Anything else you notice is outdated

### Diff against previous state

Compare available updates against existing PR descriptions:

- **New updates**: attempt to apply
- **Previously applied**: verify still present after rebase
- **Previously blocked**: re-check — has a new release resolved the issue?
- **Previously skipped as complex**: don't retry unless something changed

## Evidence Requirements

**You MUST actually try every update before classifying it.** Run `bazel build
//...` and `bazel test //...` with the update applied. Only AFTER seeing the
result can you classify the update.

### DO NOT

- Use version numbering as a proxy for "breaking" (e.g., "0.x → 0.y is semver
  breaking" without building). Many 0.x bumps are drop-in compatible.
- Reference TODO.md entries as blocking evidence unless the TODO explicitly says
  "do not upgrade this dependency".
- Claim "N call sites affected" without listing the actual files and line numbers.
- Declare an update "blocked" or "too complex" without stating specific scope.
- Write vague reasons like "API breaking changes" — name the specific APIs.

### DO

- Quote exact error messages with file path and line number.
- Include BuildBuddy invocation links for failed builds/tests.
- For API changes: quote old and new signatures, list every affected call site
  as `file.rs:42`.
- For major version bumps: read the migration guide. State what specifically
  would need to change, whether there is a replacement API, and your estimate
  of effort (e.g., "4 files, ~30 lines, mechanical rename" vs "requires
  rewriting the WebSocket connection setup across 3 modules").
- For "complex migration": give a concrete scope estimate and open a non-trivial
  PR if the migration is feasible, even if it's a larger diff.

### Examples of BAD vs GOOD blockers

Bad: `protobuf 34.0.bcr.1 → 34.1 | UPB GCC warnings (tracked in TODO.md)`
— The TODO is about pre-existing warnings on the CURRENT version. This does not
block upgrading. You must actually try the upgrade and report what happens.

Good: `protobuf 34.0.bcr.1 → 34.1 | bazel build //... fails:
external/protobuf+/upb/wire/decode.c:281 -Werror=maybe-uninitialized
(BuildBuddy: <link>). Upstream tracking: protocolbuffers/protobuf#17052`

Bad: `reqwest ^0.12 → ^0.13 | API breaking changes, 12 call sites`

Good: `reqwest ^0.12 → ^0.13 | Client::new() now returns Result instead of
panicking. 4 call sites: finance/worthy/main.rs:365,
finance/worthy/converter/fixer_converter.rs:45,
finance/worthy/ibflex.rs:280,300. Migration PR: #985`

Bad: `aspect_rules_js 2.9.2 → 3.0.3 | Major v3 breaking API: pnpm_lock_import
removed`

Good: `aspect_rules_js 2.9.2 → 3.0.3 | v3 removes pnpm_lock_import in favor
of npm_translate_lock (migration guide: <link>). Our codebase already uses
npm_translate_lock (MODULE.bazel:285,358), so pnpm_lock_import removal does
not affect us. Upgrade applied cleanly — included in bulk PR.`

## Changelog Research

**For EVERY dependency version change**, read the changelog, release notes, or
commit history between old and new version. Document anything the reviewer
should know:

- **New features** relevant to our code (could we use a new API?)
- **Bug fixes** for issues we've hit or workarounds we've applied
- **Deprecations** of APIs we currently use
- **Behavioral changes** (stricter validation, changed defaults)
- **New lint rules/checks** from linter bumps

The reviewer should understand the semantic content of every update without
reading changelogs themselves. Put highlights in the PR description "Changelog
Highlights" section and in commit messages.

Good commit message examples:

- "bump ruff 0.8→0.9: adds RUF060 (mutable-default-in-dataclass), 3 new
  findings in our code — suggest enabling in a followup"
- "bump rules_oci 2.2.7→2.3.0: new `reproducible` attr on `oci_image`, no
  action needed"
- "bump sqlalchemy 2.0.44→2.0.48: fixes asyncpg connection pool leak under
  high concurrency (we may have hit this in props)"

## Apply Updates

### Cross-ecosystem version compatibility

Some dependencies are declared in multiple ecosystems (pip, Bazel, Nix) and
their versions must remain compatible. When updating any of these, verify all
declarations.

**Protobuf** is the primary example:

| Ecosystem | Declaration                                                      | Example                          |
| --------- | ---------------------------------------------------------------- | -------------------------------- |
| Bazel     | `MODULE.bazel`: `bazel_dep(name = "protobuf", version = "33.1")` | protoc gencode 6.33.1            |
| pip       | `pyproject.toml`: `protobuf==6.33.1`                             | Python runtime                   |
| Nix       | `nix/packages/default.nix`: `protobuf` (tracks nixpkgs)          | Runtime in devShell/home-manager |

**Rule**: protobuf runtime (pip/Nix) must be **>=** gencode (Bazel protoc).
Bazel module version `X.Y` maps to gencode `6.X.Y`. If bumping Bazel protobuf,
also bump pip. If Nix lags behind, do not bump Bazel/pip past the Nix version.

When proposing ANY protobuf update, verify all three are compatible and note
the versions in the PR description.

### Lockfile regeneration by ecosystem

After editing version pins, regenerate lockfiles:

- **Python** (`pyproject.toml`): `bazel run //:requirements.update`
- **Rust** (`Cargo.toml`): `CARGO_BAZEL_REPIN=1 bazel build @crates//:all`
- **JavaScript** (`package.json`): run any Bazel build — pnpm lockfile updates
  on first build (which fails), then run again
- **Bazel modules** (`MODULE.bazel` `bazel_dep`): no lockfile regen needed
- **OCI images** (`MODULE.bazel` `oci.pull`): update both `tag` and `digest`.
  Get new digest: `crane digest <image>:<tag>`

### Batching and consolidation strategy

Fan out independent probes when that gets CI started sooner, but do not leave
every successful probe as a permanent one-package PR. Consolidate continuously:

1. **Probe** uncertain updates independently. Do not stack dozens of untested
   changes onto one branch; this preserves failure attribution.
2. **Admit** an update to a safe group only after its exact PR head has green
   required checks on RBE, with no source/API migration and no unresolved
   resolver or generated-file drift. A URL-less legacy Renovate status is not a
   substitute for required CI, but it also does not block admission when the
   actual required checks are green.
3. **Group** all admitted updates that share a compatible boundary. Ordinary
   back-compatible Python package bumps, including lockfile-only pins, are a
   canonical example. Patch/minor updates in other ecosystems can join the same
   group when they have the same version-only evidence.
4. **Separate** updates that touch source code, alter public/runtime behavior,
   require a migration guide, change exact resolver constraints, or have a
   cross-package/provider/ABI dependency. A major bump with no observed impact
   may join a safe group after proof; a major bump with a real API or behavior
   change stays in its migration PR.
5. **Validate the aggregate** from current `devel` with the normal full CI. The
   aggregate is the review artifact; the probe PRs are evidence and may be
   closed as superseded. If aggregate CI fails, bisect by removing the admitted
   update groups or restore the probe boundaries rather than abandoning all of
   them.

Do not classify an update as safe from its version number alone. Conversely, do
not keep a plainly compatible patch/minor or trivial Python bump isolated merely
because it came from a different Renovate branch.

If resuming an existing grouped PR, start from its known-good state, preserve
the source-probe links, and add only newly proven updates.

### Testing

Run `bazel build //... && bazel test //...` to verify. If something breaks:

1. Read the error carefully
2. If fixable with a small code change: fix it and include in the bulk PR
3. If it requires significant migration: create a non-trivial PR for it
4. If not feasible now: revert that update, document why with evidence

### Snapshot tests

If snapshot tests fail due to intentional output changes:

```bash
bbr test //path/to:snapshot_test \
  --test_arg=--snapshot-update \
  --nocache_test_results
```

Commit the updated `.ambr` files.

## Commit & Push

Make clean, descriptive commits with changelog highlights (see examples above).

Commit messages should include anything the maintainer should know about the
update: new features relevant to our code, deprecations, behavioral changes,
suggested followups. The maintainer should be able to review the PR by reading
commit messages without having to look up changelogs.

```bash
# Grouped safe-update PR
git push origin deps/auto-update --force

# Non-trivial PRs
git push origin deps/<package-slug> --force
```

## Create or Update PRs

```bash
# Grouped safe-update PR
gh pr create \
  --base devel \
  --title "deps: bulk dependency updates ($(date +%Y-%m-%d))" \
  --body "$(cat <<'PREOF'
<bulk PR body — see format below>
PREOF
)"

# Non-trivial PR
gh pr create \
  --base devel \
  --title "deps: migrate <package> to <version>" \
  --body "$(cat <<'PREOF'
<non-trivial PR body — see format below>
PREOF
)"
```

To update an existing PR description:

```bash
gh pr edit <NUMBER> --body "$(cat <<'PREOF'
<updated body>
PREOF
)"
```

## Verifying Tests

Follow the standard "Before Hand-off" instructions in AGENTS.md. If a test
failure is clearly pre-existing (also failing on `devel`), document it in the
PR description but do not let it block you.

## Grouped Safe-Update PR Description Format

```markdown
## Summary

**X** dependencies grouped, **Y** blocked (with evidence below),
**Z** non-trivial migrations in separate PRs.

### Updates Applied

| Package     | Old    | New    | Changelog Notes                        |
| ----------- | ------ | ------ | -------------------------------------- |
| `pydantic`  | 2.12.0 | 2.12.5 | Fixes model_copy edge case we may hit  |
| `rules_oci` | 2.2.7  | 2.3.0  | New `reproducible` attr on `oci_image` |
| `ruff`      | 0.8.0  | 0.9.0  | Adds RUF060, 3 new findings — followup |

### Source Probe Evidence

| Update                | Probe PR / tested head | Admission evidence                                       |
| --------------------- | ---------------------- | -------------------------------------------------------- |
| `pydantic-core`       | #123 / `<sha>`         | version/lockfile-only; required RBE checks pass          |
| `some-python-package` | #124 / `<sha>`         | resolver and `requirements_test` pass; no source changes |

### Non-Trivial Migration PRs

- #985 — `reqwest` 0.12→0.13 (`Client::new()` returns Result)
- #986 — `aspect_rules_js` v2→v3 (npm_translate_lock migration)

### Blocked Updates

| Package    | Current    | Available | Evidence                                                                                                                      |
| ---------- | ---------- | --------- | ----------------------------------------------------------------------------------------------------------------------------- |
| `protobuf` | 34.0.bcr.1 | 34.1      | `bazel build` fails: `decode.c:281 -Werror=maybe-uninitialized`. Upstream: protocolbuffers/protobuf#17052. [BuildBuddy](link) |

### Changelog Highlights

- **`ruff` 0.8→0.9**: adds RUF060 (mutable-default-in-dataclass), 3 new
  findings — suggest enabling in a followup
- **`sqlalchemy` 2.0.44→2.0.48**: fixes asyncpg connection pool leak

### Suggested Followups

TODOs added by this PR (grep for them in the diff):

- `TODO.md`: evaluate new ruff rules from 0.9

### Not Tracked by Renovate

| Dependency | Current | Latest | Status                               |
| ---------- | ------- | ------ | ------------------------------------ |
| `opentofu` | 1.11.2  | 1.12.0 | Not applied: may change state format |

---

<details><summary>Agent state (for next run)</summary>

Last run: YYYY-MM-DD
Grouped branch: deps/auto-update (or the current `deps/batch-*` branch)
Non-trivial branches: deps/reqwest-0.13, deps/rules-js-v3

All applied: [list with versions]
All blocked: [list with evidence summaries]
All non-trivial PRs: [list with PR numbers]

</details>
```

## Non-Trivial PR Description Format

```markdown
## Migration: `<package>` <old> → <new>

### What changed upstream

<1-3 sentences about the key changes from the changelog/migration guide>

### Code changes in this PR

<Summary of what was changed in our code and why>

### Affected files

- `path/to/file.rs:42` — changed `Client::new()` to `Client::new()?`
- ...

### Verification

- Tests: passing on RBE — [BuildBuddy invocation](link)
- Related bulk PR: #980

### Changelog excerpt

<Relevant excerpt from upstream changelog>
```

## Completion Checklist

Before declaring done, verify ALL of the following:

- [ ] Every version change has changelog research documented in the PR
- [ ] Every "blocked" update has specific evidence: BuildBuddy invocation link,
      exact error text with `file:line`, or changelog citation with API
      signature diff
- [ ] No update is blocked solely by version number pattern (0.x semver)
      without an actual build/test attempt
- [ ] No update is blocked by a TODO.md reference unless the TODO explicitly
      says "do not upgrade"
- [ ] `bazel test //...` passes on RBE for every grouped safe-update PR
      (BuildBuddy link in PR)
- [ ] Every update in a grouped PR has exact source-probe/head evidence recorded
      in that PR
- [ ] Grouped PRs contain only admitted compatible updates; red, pending, or
      semantically coupled work remains separate
- [ ] Failed aggregate CI was bisected by removing admitted update groups, not
      hidden by weakening checks
- [ ] `bazel test //...` passes on RBE for every non-trivial PR (BuildBuddy link in PR)
- [ ] Package names are in backticks in all PR description tables
- [ ] Each non-trivial PR has its own description with migration notes
- [ ] The bulk PR links to all non-trivial PRs
- [ ] The union of all PRs + documented blockers covers every Renovate
      dashboard item
- [ ] Commit messages include changelog highlights for non-patch updates
