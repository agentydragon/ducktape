# Make Cheat Critique (Calibration Test)

Generate a **new, independent critique** of the specimen code as if written by a **different, equally competent reviewer**.

**Purpose**: Create a "cheat" critique that should achieve ~100% recall when graded, to verify:

- The grading system can recognize the **same problems described differently**
- A different reviewer's style/grouping/naming doesn't harm recall
- Coverage scoring works with realistic variation
- The baseline for what "good recall" looks like

**Core Concept**: You're simulating what would happen if you gave the same code to a **different human reviewer** who:

- Finds all the same real problems (100% coverage)
- BUT describes them in their own words
- AND groups/names issues differently
- AND has their own style and priorities

**CRITICAL REQUIREMENT**: The cheat critique **MUST** identify **EVERY logical problem** the canonical covers (same code issues, same actual defects), but can (and should) look very different in how it presents them (grouping, naming, description style).

**Input**: Specimen slug (e.g., `ducktape/2025-11-22-01`)

**Output**: `cheat_critique.yaml` (annotated):

- **Different reviewer's voice**: Varied wording, style, technical depth, priorities
- **Independent issue naming**: Fresh IDs that this reviewer would choose
- **Alternative grouping**: Merge/split issues differently than canonical
- **Realistic variation**: Adjusted anchors/ranges, different emphasis
- Comments mapping each issue to its ground truth ID (for verification)
- Valid `CriticSubmitPayload` structure
- The grading CLI accepts `.yaml` directly

**Schema**: Read `CriticSubmitPayload`, `ReportedIssue`, `Occurrence`, and `LineRange` from:

- `props/core/critic/critic.py`
- `props/core/critic/models.py`

## Task

You are **role-playing as a different code reviewer** who is analyzing the same specimen code independently. Your job is to:

1. **Find all the same problems** the canonical reviewer found (same logical defects)
2. **Describe them in your own way** - different words, different framing, different grouping
3. **Write issues as YOU would naturally write them** - your IDs, your style, your priorities

This tests whether the grading system can recognize when **two reviewers independently found the same problems** even though they wrote about them differently.

### Step 1: Load Ground Truth

**Issue definitions are in the repository, not in snapshot exec:**

The canonical issue files (`.yaml`) are stored in the repository at:

- `specimens/<specimen-slug>/issues/*.yaml`

```bash
# From specimens directory:
# Count canonical issues
find <specimen-slug>/issues -name "*.yaml" | wc -l

# Read a specific issue
cat <specimen-slug>/issues/<issue-name>.yaml
```

**Use `snapshot exec` ONLY to verify the actual source code** being reviewed (not to read issue definitions):

```bash
# List source files in the hydrated specimen workspace
props snapshot exec <specimen-slug> -- find . -name "*.py" -o -name "*.ts"

# Read specific source file lines to verify issue anchors
props snapshot exec <specimen-slug> -- sed -n '10,20p' path/to/file.py
```

Parse **ALL** canonical issues in the specimen (from the repository), noting:

- Issue IDs (from filenames: `issues/iss-001.yaml` → `iss-001`)
- Rationales (original phrasing from YAML files)
- File paths and line ranges (relative paths from repo root)
- Occurrence structures (one issue can have multiple occurrences)
- The actual specimen code (to verify line ranges are plausible)

**Count the total number of canonical issues** - your cheat critique must cover all of them, but the count of reported issues may differ due to merges/splits (document these changes).

**Important grading semantics**:

- Grader uses **fuzzy LLM-based matching** (not exact string comparison)
- One reported issue can match **multiple canonical issues** (coverage)
- Multiple reported issues can overlap the **same canonical** (only counted once)
- **Coverage credits**: Grader assigns fractional credit (0-1) per canonical
  - 1.0 = fully covered, 0.5 = half covered, etc.
  - Recall = average of per-canonical credits

### Step 2: Verify All Files and Anchors

**CRITICAL**: Before paraphrasing any issue, verify that all referenced files and line ranges actually exist in the specimen:

```bash
# List all Python files to confirm structure
props snapshot exec <specimen-slug> -- find . -name "*.py" -o -name "*.ts" -o -name "*.tsx"

# For each file referenced in a canonical issue, verify it exists
props snapshot exec <specimen-slug> -- ls -l path/to/file.py

# For each line range, verify the lines exist and match what the issue describes
props snapshot exec <specimen-slug> -- sed -n '10,20p' path/to/file.py
```

**DO NOT**:

- ❌ Adjust file paths when paraphrasing (keep exact paths from canonical)
- ❌ Reference line ranges without verifying them via snapshot exec
- ❌ Invent new files that don't exist in the specimen
- ❌ Create paraphrased anchors pointing to non-existent code

**DO**:

- ✅ Use exact file paths from canonical issues (they are always correct)
- ✅ Use `snapshot exec` with sed/cat to read the actual code at line ranges before adjusting them
- ✅ Verify adjusted line ranges still point to valid code
- ✅ When adjusting ranges (expanding/contracting), check the new range exists via snapshot exec

### Step 3: Alternative Reviewer Strategy

For each ground truth issue (after verifying files exist), write how a **different reviewer** would describe it:

**Think like a different reviewer** - not just rephrasing, but genuinely different perspective:

- **What would THEY notice first?** Lead with impact vs. technical detail vs. pattern
- **What language would THEY use?** Academic vs. practical vs. terse vs. conversational
- **What would THEY emphasize?** Maintainability vs. correctness vs. performance vs. readability
- **How would THEY explain it?** Examples-first vs. principle-first vs. consequences-first

**Rationale variation strategies** (vary across issues):

- **Dense → verbose**: Expand terse descriptions with context, examples, impact
- **Verbose → terse**: Compress to just the essential problem statement
- **Technical → practical**: "Violates SRP" → "Makes changes risky when requirements evolve"
- **Practical → technical**: "Hard to test" → "Tight coupling prevents dependency injection"
- **Abstract → concrete**: "Poor separation" → "Business logic mixed with HTTP handling in line 45"
- **Concrete → abstract**: "Lines 10-20 duplicate 50-60" → "Extraction opportunity for common validation pattern"
- **Problem-focused**: "This causes X" (emphasize what's wrong)
- **Solution-focused**: "Replace with Y" (emphasize the fix)

**Location adjustments** (realistic variations):

- **Expand ranges**: Widen line ranges by 1-3 lines to include context (e.g., `[10,12]` → `[9,14]`)
- **Contract ranges**: Narrow to the exact problem line (e.g., `[50,60]` → `[55,56]`)
- **Shift slightly**: Move anchor up/down by 1-2 lines if the issue spans multiple statements
- **Split occurrences**: If one ground truth occurrence spans 20+ lines, split into 2 smaller ranges
- **Merge occurrences**: If ground truth has 3 adjacent occurrences in same file, merge into one broader range

### Step 4: YAML Structure

Write a valid `cheat_critique.yaml` file:

**Issue ID Guidelines**: Choose IDs that **THIS reviewer** would naturally write (not based on canonical IDs):

- Name what THIS reviewer sees: their framing of the problem
- Canonical: `duplicate-mount-logic` → Cheat might use: `redundant-compositor-setup` or `mounting-duplication`
- Keep them concise but meaningful: 2-4 words, hyphen-separated
- Don't mirror canonical ID structure - be independent

```yaml
# Cheat critique for calibration: <specimen>
# Generated: <timestamp>
# Canonical issues: N (ALL logical problems must be identified!)
# This review: M reported issues (different grouping/framing)
# Reviewer style: [terse/verbose/technical/practical/impact-focused/etc.]
# Variation applied: X merged, Y split, Z reframed
# Coverage map: List all canonical IDs → reported IDs below
# Expected: 0.90-1.0 recall (tests if grader recognizes same problems described differently)

issues:
  # ====================================================================
  # Canonical: canon-tp-001 (broad exception handling)
  # This reviewer's take: Focus on debugging impact, use their preferred term
  # Style: verbose, impact-focused, practical consequences
  # Location: expanded [45,47] → [44,48] to show try block
  # ====================================================================
  - id: exception-swallowing
    rationale: |
      Multiple functions catch broad exception types (Exception or bare except),
      which silences programming errors and makes debugging extremely difficult.
      When these catch legitimate bugs, the only symptom is silent failure or
      incorrect behavior far from the actual error site. Narrow to specific
      exception types (ValueError, IOError, etc.) or add logging before continue.
    occurrences:
      - files:
          src/foo.py:
            - 44
            - 48
        note: null
      - files:
          src/bar.py:
            - 102
            - 105

  # ====================================================================
  # Canonical: canon-tp-002 (subprocess shell=True security)
  # This reviewer's take: Lead with security risk, terse style
  # Style: security-focused, direct, assumes reader knows the vulnerability
  # Location: contracted [89,95] → [91,92] to just the call
  # ====================================================================
  - id: shell-injection-risk
    rationale: "subprocess.run with shell=True enables shell injection. Pass command as list."
    occurrences:
      - files:
          src/runner.py:
            - 91
            - 92
```

### Step 5: Validation

Before finalizing:

- **COMPLETE COVERAGE** (CRITICAL): Have you included **EVERY** canonical issue from the specimen?
  - Count canonical issues: `find issues -name "*.yaml" | wc -l`
  - Verify each canonical ID appears in your comments
  - Your reported issue count may be different (merges/splits are valid!)
  - **Missing even one canonical issue fails the calibration test**
- **YAML validation**: Does the file parse without errors?

  ```bash
  python -c "import yaml; yaml.safe_load(open('cheat_critique.yaml'))"
  ```

- **Schema**: Does the structure match `CriticSubmitPayload`?
- **Paths**: Are all file paths relative (not absolute) and valid?
- **Ranges**: Are line ranges plausible (within file bounds, start ≤ end)?

### Step 6: Grouping Strategy (Valid Variation)

Since the grader handles flexible mappings, you can and should vary the grouping:

- **Merge related canonicals**: Combine 2-3 similar canonical issues into one reported issue
  - Example: Merge "broad except" + "swallowed exceptions" into one "exception handling" issue
  - Comment which canonicals are merged
- **Split large canonicals**: Break one canonical with many occurrences into 2-3 focused issues
  - Example: Split "pathlib migration" into "str(path) casts" + "os.path usage"
  - Comment which canonical is being split

### Step 7: Write Output

Write **one file** in the specimen directory (`specimens/<specimen>/`):

**Annotated critique**: `cheat_critique.yaml`

Include:

- Valid YAML that matches `CriticSubmitPayload` schema
- Comments mapping to ground truth IDs
- Comments on paraphrase strategies used per issue
- Comments on location adjustments (expanded/contracted/shifted ranges)
- Comments on any merges/splits

## Notes

- **COMPLETE COVERAGE IS MANDATORY**: Every logical problem in the canonical must be identified in your cheat critique. This is non-negotiable for calibration testing.
- **Think like a different reviewer**: Don't just rephrase - reimagine how someone else would see and describe these problems
- **Issue IDs should be independent**: Don't mirror or modify canonical IDs - choose what THIS reviewer would naturally name things
- **Before submitting**: Verify every canonical problem is covered (check your coverage map)

## Example Usage

```bash
# In Claude Code, from the props directory:
/make-cheat-critique ducktape/2025-11-22-01

# Then grade it:
props snapshot-grade ducktape/2025-11-22-01 \
  --critique specimens/ducktape/2025-11-22-01/cheat_critique.yaml

# Expected output:
# recall ≥ 0.90: Excellent! Grader recognizes same problems despite different framing
```
