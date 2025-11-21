local I = import '../../specimens/lib.libsonnet';

// iss-021: Use list literal instead of repeated append pattern

I.issueOneOccurrence(
  rationale=|||
    The code constructs `ts_output` using repeated `.append()` calls when a list literal
    would be cleaner.

    **Current code (lines 268-272):**
    ```python
    ts_code = generate_typescript_from_schema(unified_schema, "AgentTypes")
    ts_output = []
    ts_output.append("// Auto-generated TypeScript types from Pydantic models")
    ts_output.append("// Do not edit manually - regenerate with: npm run codegen")
    ts_output.append("")
    ts_output.append(ts_code.strip())
    ```

    **Should be:**
    ```python
    ts_code = generate_typescript_from_schema(unified_schema, "AgentTypes")
    ts_output = [
        "// Auto-generated TypeScript types from Pydantic models",
        "// Do not edit manually - regenerate with: npm run codegen",
        "",
        ts_code.strip(),
    ]
    ```

    **Why list literal is better:**
    - More concise and readable
    - All elements visible at once without scanning multiple lines
    - Consistent with other list construction patterns in the file (e.g., line 90 `output: list[str] = []`)
    - No mutation - construct the list once rather than building it incrementally
    - Standard Python pattern for known list contents
    - Easier to reorder or modify elements

    **Context:**
    The list is immediately consumed at line 274: `output_file.write_text("\n".join(ts_output))`,
    so there's no benefit to incremental construction.
  |||,
  properties=['code-style', 'readability'],
  filesToRanges={
    'adgn/scripts/generate_frontend_code.py': [
      [268, 272],  // ts_output construction with repeated append
      [274, 274],  // Immediate consumption of ts_output
    ],
  },
)
