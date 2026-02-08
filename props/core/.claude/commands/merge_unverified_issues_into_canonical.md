# Merge unverified issues into canonical issues

@README.md

From context, you should see that we are working with some particular piece of code, and gathering issues that are present in it.

## Canonical issues format (YAML)

- Canonical issues live in `issues/*.yaml` files (one issue per file).
- Issue classification:
  - `should_flag: true` → real issues that should be flagged
  - `should_flag: false` → canonical negative (do‑not‑flag)
- Rationale should be a multi-line text block.

We have:

- **Canonical issues** in `issues/*.yaml` where we gather:
  - canonical descriptions of issues I've validated (should_flag: true),
  - canonical negatives (should_flag: false).
- A bunch of other **gathered issue files** in the same directory (freeform). They are unverified and may include duplicates.

## Main loop

Start by explaining your understanding of the context:

- Where are the **canonical issue files** (`issues/*.yaml`)
- Where are the **gathered issue files**
- Where is the source code which is being criticized by these issues

Ask me to confirm that your understanding is correct.

Once we're in sync, read each **gathered issue file** and present to me each issue it contains.
For each gathered issue:

1. Describe it and show the subject code (include multiple examples if repeated). Include code before (with the issue) and a sketched "after" when helpful. Do not write to files yet.

2. I will decide the disposition:

   If it's a true positive (should be in canon):
   - Create a new YAML issue file in `issues/`.
   - Choose filename sequentially (iss-###.yaml) and preserve ordering.
   - Set `should_flag: true`.
   - Precisely localize with file paths and line ranges.
   - Write the rationale preserving the important semantics of the user's description.
   - For multi-file issues: specify `critic_scopes_expected_to_recall` (required).

   If it's a canonical negative (false positive):
   - Create as `should_flag: false` with clear rationale and localized anchors.

   If it's a duplicate or invalid: mark it as such and do not add to canon.

3. After handling an issue, remove duplicates of the handled issue from the gathered files (or mark as merged) and proceed.
