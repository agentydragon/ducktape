---
name: update_deps
description: >
  Automated dependency updates — reads Renovate dashboard, applies safe updates,
  produces CI-passing PRs from the agent's fork. One bulk PR for trivial bumps,
  separate PRs for non-trivial migrations. Use on a schedule or manually.
---

# Automated Dependency Updates

## Your Purpose

You maintain a **set** of dependency update PRs for this monorepo.

**Invariant**: the union of all open dep-update PRs, plus documented blockers,
covers ALL current Renovate dashboard items AND any other outdated deps you find.

Two PR categories:

1. **Bulk trivial PR** (`deps/auto-update`): every update that "just works" — bump
   version, regen lockfile, build + test pass, no code changes needed.
2. **Non-trivial PRs** (`deps/<package-slug>`): one PR per update (or small related
   group) that requires code changes, API migration, or behavioral investigation.

You are **NOT done** until:

- CI passes on every open PR
- Every available update is either in a PR or has a documented blocker with
  specific evidence (see Evidence Requirements)

## PR Model

### Bulk trivial PR

- Branch: `deps/auto-update`
- Title: `deps: bulk dependency updates (YYYY-MM-DD)`
- Contains: all updates that build + test cleanly without code changes
- Links to: all non-trivial PRs in the description

### Non-trivial PRs

- Branch: `deps/<package-slug>` (e.g., `deps/reqwest-0.13`, `deps/rules-js-v3`)
- Title: `deps: migrate <package> to <version>`
- Contains: version bump + all required code changes for that migration
- Description: migration summary, API changes, changelog highlights

### Lifecycle

You manage the full set of PRs. On each run:

1. List all open `deps/*` PRs
2. Reconcile against current Renovate dashboard
3. Open, close, force-push, or update PRs as needed

## State Passing Between Runs

You are stateless. Your state lives in the PRs:

- **Bulk PR description**: tables of what was updated, what's blocked, links to
  non-trivial PRs. Your future instance reads this first.
- **Non-trivial PR descriptions**: migration details, what was tried, what worked.
- **Commit history**: shows what changes were applied.

On every run, start by reading ALL existing dep-update PR descriptions to
understand what the previous run already tried and decided. Then diff against the
current Renovate dashboard to find what's new or changed.

## Fork & Branch Setup

You run as `agentydragon-agent` without collaborator access. You work on a fork.

- **Fork**: `agentydragon-agent/ducktape`
- **Upstream**: `agentydragon/ducktape`
- **PRs**: cross-fork PRs targeting `agentydragon/ducktape` branch `devel`

```bash
# Ensure remotes
git remote get-url upstream 2>/dev/null || git remote add upstream https://github.com/agentydragon/ducktape.git
git remote get-url fork 2>/dev/null || git remote add fork https://github.com/agentydragon-agent/ducktape.git
git fetch upstream devel

# List all existing dep-update PRs
gh pr list --repo agentydragon/ducktape --author agentydragon-agent \
  --search "deps:" --state open --json number,url,headRefName,title

# Check out bulk branch (create or rebase)
git fetch fork deps/auto-update 2>/dev/null && \
  git checkout deps/auto-update && git rebase upstream/devel || \
  git checkout -b deps/auto-update upstream/devel
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

### Lockfile regeneration by ecosystem

After editing version pins, regenerate lockfiles:

- **Python** (`pyproject.toml`): `bazel run //:requirements.update`
- **Rust** (`Cargo.toml`): `CARGO_BAZEL_REPIN=1 bazel build @crates//:all`
- **JavaScript** (`package.json`): run any Bazel build — pnpm lockfile updates
  on first build (which fails), then run again
- **Bazel modules** (`MODULE.bazel` `bazel_dep`): no lockfile regen needed
- **OCI images** (`MODULE.bazel` `oci.pull`): update both `tag` and `digest`.
  Get new digest: `crane digest <image>:<tag>`

### Batching strategy

Do NOT apply all updates at once and then run a single build. If many things break
simultaneously, it's nearly impossible to isolate which update caused which failure.

Work incrementally: apply a small batch, build and test, commit what works, then
move to the next batch. How you batch is up to you — by ecosystem, by risk level,
or one-at-a-time for risky updates. The key rule: never let the tree get into a
state where you have dozens of untested changes stacked up.

If you're resuming an existing PR that already has passing updates, start from that
known-good state and add new updates incrementally on top.

### Testing

Run `bazel build //... && bazel test //...` to verify. If something breaks:

1. Read the error carefully
2. If fixable with a small code change: fix it and include in the bulk PR
3. If it requires significant migration: create a non-trivial PR for it
4. If not feasible now: revert that update, document why with evidence

### Snapshot tests

If snapshot tests fail due to intentional output changes:

```bash
bazel test //path/to:snapshot_test \
  --test_arg=--snapshot-update \
  --remote_executor="" \
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
# Bulk PR
git push fork deps/auto-update --force

# Non-trivial PRs
git push fork deps/<package-slug> --force
```

## Create or Update PRs

```bash
# Bulk PR
gh pr create --repo agentydragon/ducktape \
  --head agentydragon-agent:deps/auto-update --base devel \
  --title "deps: bulk dependency updates ($(date +%Y-%m-%d))" \
  --body "$(cat <<'PREOF'
<bulk PR body — see format below>
PREOF
)"

# Non-trivial PR
gh pr create --repo agentydragon/ducktape \
  --head agentydragon-agent:deps/<slug> --base devel \
  --title "deps: migrate <package> to <version>" \
  --body "$(cat <<'PREOF'
<non-trivial PR body — see format below>
PREOF
)"
```

To update an existing PR description:

```bash
gh pr edit <NUMBER> --repo agentydragon/ducktape --body "$(cat <<'PREOF'
<updated body>
PREOF
)"
```

## CI Polling

After pushing a commit intended as "final" on any PR, you **MUST** poll CI
until completion:

```bash
# Wait for checks (with timeout)
gh pr checks <NUMBER> --repo agentydragon/ducktape --watch --fail-fast

# Or poll manually
gh pr checks <NUMBER> --repo agentydragon/ducktape
```

If CI fails:

1. Download and read the failure logs
2. Diagnose the root cause
3. Fix the issue (code change, revert a problematic update, etc.)
4. Push again
5. Poll again

**You are NOT done until CI passes on all PRs.** This may require multiple
fix-push-poll cycles. If a test failure is clearly pre-existing (also failing
on `devel`), document it in the PR description but do not let it block you.

## Bulk PR Description Format

```markdown
## Summary

**X** dependencies updated, **Y** blocked (with evidence below),
**Z** non-trivial migrations in separate PRs.

### Updates Applied

| Package     | Old    | New    | Changelog Notes                        |
| ----------- | ------ | ------ | -------------------------------------- |
| `pydantic`  | 2.12.0 | 2.12.5 | Fixes model_copy edge case we may hit  |
| `rules_oci` | 2.2.7  | 2.3.0  | New `reproducible` attr on `oci_image` |
| `ruff`      | 0.8.0  | 0.9.0  | Adds RUF060, 3 new findings — followup |

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

- `x/agent_server/TODO.md`: drop `async_depends` wrapper after fastapi 0.116
- `TODO.md`: evaluate new ruff rules from 0.9

### Not Tracked by Renovate

| Dependency | Current | Latest | Status                               |
| ---------- | ------- | ------ | ------------------------------------ |
| `opentofu` | 1.11.2  | 1.12.0 | Not applied: may change state format |

---

<details><summary>Agent state (for next run)</summary>

Last run: YYYY-MM-DD
Bulk branch: deps/auto-update
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

- CI: [passing](link)
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
- [ ] CI passes on the bulk trivial PR
- [ ] CI passes on every non-trivial PR
- [ ] Package names are in backticks in all PR description tables
- [ ] Each non-trivial PR has its own description with migration notes
- [ ] The bulk PR links to all non-trivial PRs
- [ ] The union of all PRs + documented blockers covers every Renovate
      dashboard item
- [ ] Commit messages include changelog highlights for non-patch updates
