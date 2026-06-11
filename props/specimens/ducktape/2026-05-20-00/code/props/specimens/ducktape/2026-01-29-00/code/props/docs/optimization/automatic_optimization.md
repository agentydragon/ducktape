# Automatic Prompt Optimization

## Core Pattern: Generate-Evaluate-Refine

All automatic optimization follows this loop:

```
while not converged:
    prompts = generate_candidates(feedback)
    scores = evaluate_on_examples(prompts)
    feedback = analyze_performance(scores)
```

## Our Approach: GEPA

**Generate, Evolve, Prioritize, Analyze:**

1. **Generate:** Create prompt variants from baseline
2. **Evaluate:** Test on train examples (per-file and full-snapshot)
3. **Evolve:** Analyze failures → propose targeted improvements
4. **Prioritize:** Rank by validation LCB (penalizes variance)
5. **Repeat:** Iterate until convergence

**You are the "reflection LM"** — your job is to analyze failures and propose improvements.

## Best Practices

### 1. Start from Baseline

Don't start from scratch. Fetch the base critic and iterate.

### 2. Train/Valid Split

- **Train:** Iterate rapidly, inspect results, diagnose failures
- **Valid:** Test only after train performance looks promising
- **Never:** Optimize on valid (leads to overfitting)

**Red flag:** High train, zero valid → overfit

### 3. Rich Feedback

Don't just look at accuracy numbers. Analyze:

- **What failed:** Specific examples the prompt missed
- **Execution traces:** Tool calls, reasoning, where critic got stuck
- **Patterns:** "Missed all duplication issues" not just "82% accuracy"

### 4. Measure Variance

- **LCB (Lower Confidence Bound):** mean - σ/√n — penalizes high variance
- **Zero-recall %:** How often does prompt fail completely?
- Don't trust point estimates with n < 5

### 5. Budget Allocation

- **Exploration:** Many prompts × few examples (find good regions)
- **Exploitation:** Few prompts × many examples (refine winners)

## Key Design Choices

1. **Baseline-driven:** Always compare to current best validation recall. Any improvement → new baseline.

2. **Two-distribution problem:** Train has mixed difficulty (single-file, multi-file, full-snapshot). Valid is ONLY full-snapshot (hardest). Test on full-snapshot train as proxy before validation.

3. **Rich diagnostics:** Use `events` table for execution traces. Tool call sequences tell you where critic got stuck.

4. **Statistical rigor:** Small validation set = high variance. Use LCB to rank prompts.

5. **Custom scripting:** Write analysis scripts in `/workspace/`. Form hypotheses, test via custom queries.

## Proxy vs Terminal Metrics

**Terminal metric:** Whole-snapshot recall on VALID split

- What we actually care about - can the critic find issues in a real whole-repo review?
- Black-box: only aggregate recall visible, no ground truth or traces

**Proxy metric:** File-set recall on TRAIN split

- Easier to debug (1-5 occurrences vs hundreds)
- Full debugging access: TPs, execution traces, everything
- Improvements here _hopefully_ transfer to terminal metric

**Workflow:**

```
File-set on TRAIN (proxy)  →  Whole-snapshot on TRAIN  →  Whole-snapshot on VALID (terminal)
Easy to debug, small scope    Harder, more occurrences    Black-box, true generalization
```

## Hill-Climbing on Easy Examples

Start with small examples, not whole-snapshot:

```sql
-- Find easiest file-set examples (1-3 occurrences in expected recall scope)
SELECT snapshot_slug, files_hash, n_recall_denominator
FROM examples
WHERE example_kind = 'file_set'
  AND n_recall_denominator BETWEEN 1 AND 3
ORDER BY n_recall_denominator;
```

**Strategy:**

1. Pick file-set examples with 1-3 occurrences
2. Run critic, grade, analyze what was missed
3. Much easier to debug "why did I miss 1 thing in 2 files?" than "why did I miss 259 things?"
4. Iterate until catching small examples consistently
5. Expand to larger file-sets, then whole-snapshot

Whole-snapshot is your terminal metric, not your iteration target.

## Debugging Low Recall

When recall is low or zero, **diagnose before giving up**. You have full debugging access:

```sql
-- What did the critic report?
SELECT issue_id, rationale FROM reported_issues
WHERE agent_run_id = '<critic_run_id>';

-- What were the TPs it should have found?
SELECT tp_id, rationale FROM true_positives
WHERE snapshot_slug = '<snapshot>';

-- How was each reported issue graded?
SELECT * FROM grading_edges
WHERE grader_run_id = '<grader_run_id>';

-- What did the critic actually do? (execution trace)
SELECT event_type, payload FROM events
WHERE agent_run_id = '<critic_run_id>'
ORDER BY sequence_num;
```

**Diagnose before iterating:**

1. Compare reported issues to TPs - what patterns are missing?
2. Read execution traces - did the critic analyze the right files?
3. Check if "novel" findings align with labeler preferences
4. Then modify the definition to address specific gaps

## When to Use report-failure

**Infrastructure failures only:**

- Definition won't build (Dockerfile errors)
- Database inaccessible
- Systematic errors preventing any evaluation

**NOT for metrics:**

- 0% recall → diagnose and iterate
- All "novel" findings → investigate
- Any numeric result → it's feedback, not failure
