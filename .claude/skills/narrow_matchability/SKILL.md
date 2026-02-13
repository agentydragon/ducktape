---
name: narrow_matchability
description: Propose narrowing of grader match_file_restriction for unrestricted TP/FP occurrences in a specimen. Produces verifiable, link-rich output that lets the user confirm each restriction is correct.
argument-hint: "<snapshot_slug>"
allowed-tools: Bash, Read, Grep, Glob, Task, WebFetch
---

# Narrow Grader Matchability

Analyze unrestricted (`match_file_restriction IS NULL`) true positive and false
positive occurrences in a specimen snapshot, and propose narrowed file
restrictions with **verifiable proofs of correctness**.

**Argument:** `$ARGUMENTS` (a snapshot slug, e.g. `gmail-archiver/2025-12-17-00`)

## Background

Read these before starting — they define `match_file_restriction` semantics and
contain labeled positive/negative examples of correct restriction assignments:

- @props/specimens/docs/format-spec.md (section "Match File Restriction")
- @props/specimens/docs/only-matchable-labels.md (labeled examples with reasoning)

The **validation test** from `only-matchable-labels.md` is the key correctness
criterion: "Can you produce a valid critique phrasing that accurately describes
this issue but tags a file outside the proposed set?" If yes, the set is too
narrow.

## Step 0: Ensure Database is Available

This skill requires a running props database with synced specimen data. Before
querying, verify the database is reachable:

```bash
psql -Atc "SELECT count(*) FROM true_positive_occurrences;" 2>&1
```

If the database is not available (connection refused, table doesn't exist, etc.),
use the `test_props` skill with `setup` argument to start infrastructure and
initialize the database. That skill handles starting PostgreSQL via podman,
creating the schema, and syncing all specimen data.

## Step 1: Identify Unrestricted Occurrences

Use `psql` (reads `PG*` env vars from the current shell automatically).

If the user provided a snapshot slug as `$ARGUMENTS`, use it. Otherwise, query
for all slugs with unrestricted occurrences:

```bash
psql -Atc "
  SELECT DISTINCT snapshot_slug
  FROM true_positive_occurrences
  WHERE match_file_restriction IS NULL
  UNION
  SELECT DISTINCT snapshot_slug
  FROM false_positive_occurrences
  WHERE match_file_restriction IS NULL
  ORDER BY 1;
"
```

Then for the target slug(s), pull unrestricted TP and FP occurrences:

```bash
psql -Atc "
  SELECT 'TP', tpo.tp_id, tpo.occurrence_id
  FROM true_positive_occurrences tpo
  WHERE tpo.snapshot_slug = '<slug>'
    AND tpo.match_file_restriction IS NULL
  UNION ALL
  SELECT 'FP', fpo.fp_id, fpo.occurrence_id
  FROM false_positive_occurrences fpo
  WHERE fpo.snapshot_slug = '<slug>'
    AND fpo.match_file_restriction IS NULL
  ORDER BY 1, 2, 3;
"
```

If zero unrestricted occurrences exist, report that and stop.

## Step 2: Read Each Issue YAML

For each TP/FP with unrestricted occurrences, read the issue YAML from
`props/specimens/<slug>/issues/<issue_id>.yaml`. Extract `files`,
`critic_scopes_expected_to_recall` (TP) or `relevant_files` (FP), and
`rationale`.

## Step 3: Propose Restrictions with Proofs

For each unrestricted occurrence, propose a `match_file_restriction` value and
provide a **proof of completeness** — evidence that no valid reporting files are
missing from the proposed set.

### Proof requirements

The proof must let the user verify correctness by reading your output alone,
without independent research.

**For single-file issues** (one file in `files:`, local code pattern): the
proof is mechanical — restrict to that file. Present these as a compact table.

**For multi-file or cross-cutting issues**: grep the specimen's `code/`
directory for the relevant symbol/pattern and show all matches. The user needs
to see the search was thorough. Key questions:

1. Could a valid critique mention a file outside the proposed set?
   (See negative examples in `only-matchable-labels.md` — dual-framing,
   producer/consumer relationships, cross-file duplication)
2. Are there other files in the specimen with the same pattern that should be
   covered by some occurrence's restriction?

**When in doubt, include the file.** False negatives (missing a valid file) are
worse than false positives (including an extra file). Flag uncertain cases with
"NEEDS REVIEW".

### Output format per occurrence

```markdown
#### `<issue_id>` / `<occurrence_id>`

[YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/<slug>/issues/<issue_id>.yaml)

**Issue:** <one-line summary>
**Code location:** `<file>:<lines>`
**Proposed restriction:** `[<file1>, <file2>, ...]`

**Proof:** <evidence — grep results, cross-references, or reasoning>
```

Group single-file mechanical proposals into a compact table. Present multi-file
proposals individually with full proof.

### Summary

End with counts: total unrestricted, proposed restrictions (single-file vs
multi-file), and any flagged for review.

## Important Notes

- File paths use the specimen-relative prefix (not absolute repo paths)
- GitHub links use the `devel` branch
- Only narrow NULL → specific set; never remove an existing restriction
- Grep the specimen's `code/` directory, not the whole repo
