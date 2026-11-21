# Evaluation Flow: Critic → Grader → Metrics

This document describes the end-to-end evaluation pipeline for prompt optimization.

## Pipeline Overview

```
Critic Run                    Grader Run                 Metrics
───────────                   ──────────                 ───────
Source code ─┐
             ├─> Critique ────> Match to TPs/FPs ────> Recall
System prompt┘   (reported_issues)  (grading_edges)        (aggregate views)
```

${describe_relation("snapshots")}
${describe_relation("snapshot_files")}

## 1. Critic Run

**What the critic sees:**
- Source code fetched to `/workspace` at startup (writable)
- System prompt with task description
- Database access for writing issues

**What the critic DOES NOT see:**
- Ground truth issues (TPs/FPs)
- Expected output or "answers"
- Grader feedback or metrics

**Critic's task:** Review code, report issues, and call submit when done.

## 2. Grader Run

**Input:**
- Critique from critic (query `reported_issues`, `reported_issue_occurrences`)
- Ground truth from snapshot (query `true_positives`, `true_positive_occurrences`, `false_positives`, `false_positive_occurrences`)

**Task:** Fill in a bipartite graph connecting critique issues to GT occurrences.

Every (critique_issue, matchable_gt_occurrence) pair needs an edge with credit:
- **1.0** = perfect match (issue accurately describes occurrence)
- **0.0** = no match (issue doesn't describe occurrence)
- **Partial** = issue partially captures the occurrence

**Matchability:** An occurrence is matchable from a critique issue if:
- No file restriction is specified (`match_file_restriction IS NULL`, i.e. unrestricted), OR
- The critique touches files that overlap with the occurrence's file scope (file-restricted)

**Output:** `grading_edges` table populated with edges between issues and GT occurrences.

## 3. Metrics Computation

**Per-run:** Recall = sum(found_credit) / count(occurrences)

**Aggregate:** Query views like `recall_by_definition_split_kind`

## Recall Views

${describe_relation("recall_by_definition_split_kind")}
${describe_relation("recall_by_definition_example")}

### Weighting

Cross-run recall is weighted by occurrence, not by example:
- Formula: `SUM(AVG(found_credit) per occurrence) / COUNT(occurrences)`
- Examples with more TP occurrences naturally weight more
- This measures "total issues found / total issues in dataset"

### Failure Handling

When a critic container crashes or times out:
- Any critique items produced before failure are still graded
- Recall is computed from whatever was reported (may be partial)
- A run with no reported issues gets zero recall

### Pareto Frontier (Best Definitions per Example)

${describe_relation("pareto_frontier_by_example")}

Use this to find:
- Which definitions achieve best recall on each example
- Difficult examples where even the best definition struggles
- Definition specialization patterns

```sql
-- Find hardest examples (lowest best recall)
SELECT snapshot_slug, files_hash, best_mean_credit
FROM pareto_frontier_by_example
WHERE split = 'train'
ORDER BY best_mean_credit ASC
LIMIT 10;

-- Which definitions win most often on validation?
SELECT w->>'image_digest' AS def_id, COUNT(*) AS wins
FROM pareto_frontier_by_example, jsonb_array_elements(winning_definitions) AS w
WHERE split = 'valid'
GROUP BY def_id ORDER BY wins DESC;
```

## Validation Recall

${describe_relation("validation_recall_by_definition")}

## Data Access Patterns

### Training Split (Full Access)

```sql
-- Get examples
SELECT * FROM examples e
JOIN snapshots s ON e.snapshot_slug = s.slug
WHERE s.split = 'train';

-- Access ground truth
SELECT * FROM true_positives WHERE snapshot_slug = '<slug>';
```

### Validation Split (Restricted)

```sql
-- Query aggregate metrics only
SELECT * FROM recall_by_definition_split_kind
WHERE split = 'valid' AND critic_image_digest = '<def_id>';
```

**IMPORTANT:** Always check `n_examples >= 5` before trusting metrics (small samples = high variance).
