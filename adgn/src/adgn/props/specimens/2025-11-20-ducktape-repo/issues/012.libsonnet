local I = import '../../specimens/lib.libsonnet';

// iss-012: Use walrus operator to inline eval_index creation

I.issueOneOccurrence(
  rationale=|||
    Lines 580-581 separate the creation of `eval_index` from its immediate use in the
    write_text call. The variable is needed later (for the loop at line 592 and return
    at line 596), but the creation can be inlined into the write call using the walrus
    operator.

    **Current code (lines 580-581):**
    ```python
    eval_index = EvalIndex(samples=list(entries))
    (root / "index.json").write_text(eval_index.model_dump_json(indent=2), encoding="utf-8")
    ```

    **Should be:**
    ```python
    (root / "index.json").write_text((eval_index := EvalIndex(samples=list(entries))).model_dump_json(indent=2), encoding="utf-8")
    ```

    **Why inline with walrus operator?**
    - Combines creation and first use into one line
    - eval_index remains available for subsequent uses (loop, return)
    - More concise without sacrificing readability
    - Standard Python 3.8+ pattern for "create and immediately use" scenarios
  |||,
  properties=['code-style', 'readability'],
  filesToRanges={
    'adgn/src/adgn/props/eval_harness.py': [
      [580, 581],  // eval_index extraction and immediate write use
    ],
  },
)
