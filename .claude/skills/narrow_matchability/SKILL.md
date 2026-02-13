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

**CRITICAL: Do NOT assume that one file in `files:` means the restriction is
that file.** The `files:` field records where the _problematic code_ is, but a
valid critique could tag a _different_ file — a caller, consumer, duplicate, or
the other side of a producer/consumer relationship. See the
`dead-constants-runs-context` negative example in `only-matchable-labels.md`:
`files:` has only `runs_context.py`, but a critic could validly tag the files
with hardcoded strings instead.

For **every** occurrence (regardless of how many files are in `files:`):

1. **Understand what the issue complains about** — read the rationale and the
   actual code at the referenced lines. Show a brief code snippet in the output.
2. **Apply the validation test**: "Can you produce a valid critique phrasing
   that accurately describes this issue but tags a file outside the proposed
   set?" Consider:
   - Dual framing (caller vs callee, producer vs consumer)
   - Cross-file duplication (same pattern in other files)
   - Import/usage sites that manifest the bug
3. **Provide evidence**: Show the relevant code snippet(s) and explain _why_
   the issue can only be validly reported on the proposed file(s). If other
   files are involved (callers, consumers, duplicates), grep for them and show
   the results.

**When in doubt, include the file.** False negatives (missing a valid file) are
worse than false positives (including an extra file). Flag uncertain cases with
"NEEDS REVIEW".

### Output format per occurrence

```markdown
#### `<issue_id>` / `<occurrence_id>`

**Issue:** <one-line summary of what the rationale complains about>

Links: [YAML](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/<slug>/issues/<issue_id>.yaml) · [code](https://github.com/agentydragon/ducktape/blob/devel/props/specimens/<slug>/code/<file_path>#L<start>-L<end>)

**Code snippet** (the relevant lines from the specimen):
` `` `
<brief code snippet showing the problematic pattern>
` `` `

**Proposed restriction:** `[<file1>, <file2>, ...]`

**Validation test:** <explain why a valid critique cannot tag a file outside
the proposed set — or flag NEEDS REVIEW if uncertain>
```

Present each occurrence individually with its code snippet and validation
reasoning. Do not use compact tables — every proposal needs the validation
test applied explicitly.

### Summary

End with counts: total unrestricted, proposed restrictions (single-file vs
multi-file), and any flagged for review.

## Important Notes

- File paths use the specimen-relative prefix (not absolute repo paths)
- GitHub links use the `devel` branch
- Only narrow NULL → specific set; never remove an existing restriction
- Grep the specimen's `code/` directory, not the whole repo
