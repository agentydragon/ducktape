---
name: lint_audit
description: "Audit and improve linter/checker configuration across any repo and language. Discovers languages, proposes useful checks to enable with real examples, creates PRs for approved checks, documents rejected ones. Also fixes misconfigurations, updates outdated versions, removes stale exclusions, and proposes tightenings."
argument-hint: "[focus area, e.g. 'python', 'security', 'all']"
allowed-tools: Agent, Bash, Read, Write, Edit, Grep, Glob, WebFetch, WebSearch
model: opus
---

# Lint Audit

Comprehensive linter/checker audit for any repository. Discovers what's in use,
proposes improvements, and implements approved changes as individual PRs.

**Argument:** `$ARGUMENTS` (optional focus area — a language, category like
"security", or "all" for everything)

## Phase 1: Discovery

Gather the full picture before proposing anything.

### 1a. Detect languages and build system

Scan the repo root for:

- **Python**: `pyproject.toml`, `ruff.toml`, `.flake8`, `setup.cfg`, `mypy.ini`, `.mypy.ini`, `pyrightconfig.json`, `pylintrc`
- **JavaScript/TypeScript**: `eslint.config.*`, `.eslintrc.*`, `tsconfig.json`, `biome.json`, `prettier.config.*`
- **Rust**: `Cargo.toml`, `clippy.toml`, `.clippy.toml`, `rustfmt.toml`
- **Go**: `golangci.yml`, `.golangci.yml`
- **Shell**: `.shellcheckrc`, `shfmt` in pre-commit
- **Bazel**: `buildifier` in pre-commit, `.buildifier.json`
- **General**: `.pre-commit-config.yaml`, `.github/workflows/*.yml` (CI lint steps), `Makefile` lint targets
- **Docker**: `hadolint.yaml`, `.hadolint.yaml`
- **Terraform**: `.tflint.hcl`, `checkov` config
- **Nix**: `nixfmt`, `statix` in config

For each tool found, record:

- Current version
- Latest available version
- Configuration file path
- Which rules/checks are enabled vs available

### 1b. Catalog current state

For each linter found:

1. **Read the config** to understand what's enabled, ignored, and per-file-ignored
2. **Check for documented exclusions** — comments explaining why rules are off
3. **Check for version pins** — are they current?
4. **Check for stale exclusions** — run the excluded rules to see if they'd now pass (0 violations = exclusion can be removed)
5. **Check for misconfigurations** — duplicate entries, conflicting settings, invalid TOML/YAML, rules that don't exist in the current version
6. **Find existing cleanup PRs** — search open PRs for lint/check-related changes

### 1c. Find candidate checks

For each linter, enumerate rules/checks that are:

- Available in the installed version but not enabled
- Not already documented as intentionally excluded

For each candidate, count actual violations in the repo. Skip candidates with 0 violations (mention them as "free guardrails" separately).

## Phase 2: Present findings

Organize findings into categories, presented to the user in descending order of
value:

### Category A: Misconfigurations and staleness

- Version bumps available (current vs latest)
- Invalid/duplicate config entries
- Stale exclusions (rule excluded but 0 violations — can be removed)
- Stale per-file-ignores (file no longer triggers the rule)
- Conflicting settings between tools

### Category B: Free guardrails

Checks with 0 current violations that can be enabled as pure guardrails (no code
changes needed). List them all — these are always worth enabling.

### Category C: Checks worth enabling

For each candidate check with violations, present:

````
### {rule_code} — {short_description} ({violation_count} violations)

{Why this matters for THIS repo, referencing style guide if one exists}

**Example 1** — `path/to/file.py:42`:
```{lang}
# Before
{actual code from repo}

# After
{fixed code}
````

**Example 2** — `path/to/other.py:87`:
...

**Effort:** {Low/Medium/High} — {auto-fixable? mechanical? needs judgment?}

```

Sort by: (value to repo × ease of fixing) descending. Consider:
- Does the repo's style guide or AGENTS.md mention related conventions?
- How many violations? (more = more impact)
- Auto-fixable? (lower effort)
- Does it catch real bugs vs style preferences?

### Category D: Existing cleanup PRs
If open PRs already propose lint changes, mention them so the user can batch.

### Category E: Tightenings
Opportunities to make existing rules stricter:
- Lowering complexity thresholds
- Removing broad per-file-ignores that could be narrowed
- Enabling stricter modes of already-enabled rules

## Phase 3: User decisions

After presenting, ask the user which checks to act on. For each decision:

- **Enable**: Create a branch, add the rule to config, fix all violations, verify
  clean, commit, push, create PR with `[LINT]` prefix
- **Skip with documentation**: Add a comment to the config explaining why not enabled
- **Tighten**: Apply the tightening, fix new violations, PR
- **Fix misconfiguration**: Fix and PR
- **Remove stale exclusion**: Remove and PR

### PR conventions

- One PR per check/fix (independent, reviewable separately)
- Branch naming: `claude/lint-{tool}-{rule}` (e.g., `claude/lint-ruff-TC`,
  `claude/lint-eslint-no-unused-vars`)
- PR title: `[LINT] Enable {rule} ({tool}): {short description}`
- PR body: violation count, 2-3 examples, effort level
- Each PR must pass the repo's CI/pre-commit before submission
- Config changes must not drop existing rules or exclusions (verify diff carefully)

### When fixing violations

- **Do not change logic** — only fix the lint violation
- **Preserve existing behavior** — if unsure, use `# noqa` / `// eslint-disable` with a comment
- **Check call sites** when renaming or changing signatures
- **Run the repo's test suite** on changed files if feasible
- **Format after fixing** — run the repo's formatter

## Phase 4: Verify

After all PRs are created:
1. List all PRs with their status
2. Verify each branch's config diff adds exactly one rule (no duplicates, no dropped rules)
3. Verify each branch passes its rule check with 0 violations
4. Report any remaining candidates the user hasn't decided on

## Language-specific knowledge

### Python (ruff)
- `ruff check --select RULE --config CONFIG` to count violations
- `ruff check --select RULE --fix` for auto-fixable rules
- `ruff.toml` or `[tool.ruff]` in `pyproject.toml`
- Key high-value rules by category:
  - **Bug prevention**: B (bugbear), TRY (exception handling), BLE (blind except), PGH (type-ignore codes)
  - **Dead code**: ERA (commented code), ARG (unused args), F841 (unused vars)
  - **Modernization**: UP (pyupgrade), FA (future annotations), FURB (refurb)
  - **Performance**: PERF, C4 (comprehensions)
  - **Type safety**: TC (type-checking imports), ANN (annotations)
  - **Style**: SIM (simplify), RET (returns), PTH (pathlib), N (naming)
  - **Security**: S (bandit), DTZ (datetime timezone)
  - **Framework**: FAST (FastAPI), DJ (Django), AIR (Airflow)
  - **Logging**: G (format), LOG (calls), TRY400/401 (exception logging)
  - **Boolean hygiene**: FBT (boolean trap)

### JavaScript/TypeScript (eslint)
- `npx eslint --rule '{rule: error}' .` to count violations
- Check `eslint.config.js` or `.eslintrc.*`
- Key rules: `@typescript-eslint/strict`, `no-unused-vars`, `no-explicit-any`,
  `prefer-const`, `no-floating-promises`

### Rust (clippy)
- `cargo clippy -- -W clippy::all` for all warnings
- Check for `#![allow(...)]` in lib.rs/main.rs
- Key lint groups: `clippy::pedantic`, `clippy::nursery`, `clippy::cargo`

### Go (golangci-lint)
- Check `.golangci.yml` for enabled/disabled linters
- Key linters: `errcheck`, `staticcheck`, `gosec`, `gocritic`, `exhaustive`

### Shell (shellcheck)
- Check for `.shellcheckrc` or inline directives
- SC2086 (unquoted variables), SC2155 (declare+assign), SC2164 (cd without ||)

### Pre-commit
- Check hook versions vs latest
- Look for hooks that could be added: `check-merge-conflict`, `check-ast`,
  `trailing-whitespace`, `end-of-file-fixer`, `check-yaml`, `check-toml`

## Important constraints

- **Never drop existing rules** — only add or document
- **Never weaken existing checks** — only tighten or maintain
- **Respect documented exclusions** — if a rule has a comment explaining why it's
  off, don't re-propose it unless you have a concrete way to avoid the stated problem
- **One PR per change** — don't bundle unrelated checks
- **Verify config diffs** — after every rebase, check the config file hasn't
  accumulated duplicates or lost existing rules
- **Test before pushing** — run the check to verify 0 violations before committing
```
