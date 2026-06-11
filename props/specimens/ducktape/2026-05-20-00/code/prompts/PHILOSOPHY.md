# Scan Prompt Philosophy

## Core Principle

**Goal: Find ALL instances of the issue (100% recall). Use automated tools strategically to gather candidates, then verify with appropriate level of scrutiny.**

All scan prompts in this directory follow a common philosophy: combine automated discovery with intelligent verification to achieve comprehensive cleanup.

## The Real Goal: 100% Recall

When running a scan, the objective is **not missing any real issues**. This means:

- **Automated tools are essential** for discovery - they gather candidates/starting points
- **Some patterns have 100% recall** - "can ONLY happen in places matching this grep/AST check"
- **Tools have different characteristics** - high-recall/low-precision, high-precision/low-recall
- **Agent uses intelligence** to combine tools effectively based on their reliability

## Understanding Tool Characteristics

Different automated tools have different reliability profiles:

### High Recall, High Precision (100%/100%)

**Example**: Finding all `cast()` calls

- Every `cast()` usage matches `grep "cast("` or AST check for Call nodes with func.id == 'cast'
- Very few false positives (only matches actual cast calls)
- **Strategy**: Run tool, verify all candidates

### High Recall, Low Precision (100%/30%)

**Example**: Finding trivial forwarders

- AST can find all single-return functions (100% recall)
- Many are legitimate wrappers (70% false positives)
- **Strategy**: Run tool, manually/LLM filter candidates

### Medium Recall, High Precision (60%/90%)

**Example**: Finding manual dict serialization

- Can search for `dict[str, Any]` + `.items()` patterns
- Misses clever variations but rarely wrong
- **Strategy**: Run tool, light verification, accept some misses

### Low Recall, Manual Required (30%/N/A)

**Example**: Finding "vague" field names

- Automation can flag short names, but misses contextual vagueness
- Human judgment required for what's "vague"
- **Strategy**: Use automation for hints, manual reading required

## The Automation Strategy

✅ **Correct Approach**:

```
1. Understand the issue pattern and its characteristics
2. Run automated scans to gather candidates (grep/AST/ruff/vulture/etc.)
3. Understand recall/precision of each tool for this pattern
4. Verify candidates with appropriate scrutiny:
   - High precision: light verification
   - Low precision: careful manual review or LLM filtering
5. Fix confirmed issues
6. For low-recall patterns: supplement with manual reading
```

❌ **Bad Approach**:

```
1. Run grep/AST scanner
2. Auto-fix everything it finds without verification
3. Declare "all clean!" (false confidence)
```

**Problems with bad approach**:

- Breaks good code (low precision without verification)
- Misses issues (low recall without supplemental reading)
- False confidence (assumes tool found everything)

## Automated Tools: Essential for Discovery

### What Automated Tools ARE Good For

1. **Finding candidates** - "Here are 50 places with `cast()`, go check them" (may be 100% recall!)
2. **Pattern discovery** - "These 10 files have similar structure"
3. **Preprocessing for LLM** - Generate skeleton files, extract specific patterns
4. **Consistency checking** - Find all uses of deprecated API (often 100% recall)
5. **Heuristic filtering** - Narrow down from 1000 files to 20 worth reading

### What Automated Tools NEED Help With

1. **Verification** - Determining if candidate is actually wrong (context and judgment required)
2. **Understanding intent** - Is this a workaround, migration, or antipattern?
3. **Subjective judgment** - Is documentation "useless"? Is a name "vague"? (human/LLM verification needed)
4. **Low-recall patterns** - May need supplemental manual reading to find all issues

## Prompt Writing Style: You-Language

**All scan prompts are written in second person ("you") targeting the agent executing the scan.**

Prompts are instructions to agents/LLMs, so address the reader directly:

### ✅ GOOD: Direct you-language

```markdown
**Manual review workflow**:

1. Review dict_literals - check context
2. You analyze pydantic_models for overlapping fields
3. Filter models where len(fields) == 1
4. You compare field sets across models

**Tool characteristics**:

- Tool surfaces raw data; you do the analysis
- You filter candidates based on context
- Requires your judgment to determine if problematic
```

### ❌ BAD: Third-person LLM references

```markdown
**Manual review workflow**:

1. LLM reviews dict_literals
2. LLM does this analysis
3. LLM filters models
4. LLM compares field sets

**Tool characteristics**:

- Tool surfaces raw data; LLM analyzes it
- LLM filters candidates
- Requires human judgment
```

**Why you-language?**

- **Clearer**: Direct instructions are easier to follow than descriptions
- **Natural**: Prompts are imperative ("do this") not descriptive ("someone does this")
- **Consistent**: Matches command tone ("Run scan", "Verify candidates")

**Examples**:

- "LLM can identify overlaps" → "You can identify overlaps"
- "LLM does the filtering" → "You do the filtering"
- "Requires human judgment" → "Requires your judgment"
- "LLM verification approach" → "Verification approach" (implied you follow it)

## Mandatory vs Optional Automated Scans

**Principle**: Prompts should specify when automated scans are MANDATORY vs OPTIONAL to force agents to gather concrete candidates rather than being lazy.

### When to Make Scans MANDATORY

**Requirement**: Every scan prompt should have at least one mandatory scan step (or more).

**Goal**: Mandatory steps should achieve **high recall** (ideally include all true positives), even if precision is low. The purpose is to surface ALL files and locations that need checking, preventing the agent from checking 3 files and declaring "done" when there are actually 20 more.

Automated scans should be **required as the first step** when:

1. **High-recall discovery is possible**
   - Tool can find most or all instances of the pattern (even with false positives)
   - Agent gets a comprehensive list of candidates to review
   - **Example**: `grep "cast("` finds ALL cast() calls (100% recall, high precision)
   - **Example**: AST scan for single-return functions finds ALL trivial forwarder candidates (100% recall, 30% precision after filtering)
   - **Language**: "MANDATORY Step 0: Run scan to find ALL candidates"

2. **Prevents "I checked a few files" laziness**
   - Without mandatory scan, agent might check 3-5 files and stop
   - Mandatory scan forces agent to see the full scope (e.g., "found 47 instances")
   - Agent must at least acknowledge all candidates, can't pretend they don't exist
   - **Example**: Finding all `os.environ` manipulation in tests - agent sees all 23 test files, not just the first 3
   - **Language**: "Do not skip this step - prevents checking only a few files"

3. **Makes comprehensive review tractable**
   - Scan outputs specific line numbers/files for ALL candidates
   - Agent reviews concrete instances rather than guessing where to look
   - Prevents "I read a few files" when pattern exists in many more
   - **Example**: `scan_comments.py` outputs all 200 comments with line numbers - agent must review all, not guess where comments might be
   - **Language**: "This step is required to surface all locations requiring review"

**Key insight**: Mandatory scans force the agent to confront the full scope of work. Even if the scan has 50% false positives, it ensures the agent knows about all 20 files that need checking, not just the 3 they happened to look at.

### When to Make Scans RECOMMENDED

Automated scans should be **suggested but not required** when:

1. **Helpful but agent likely won't skip**
   - Grep patterns make search faster but agent would search anyway
   - Saves time but not essential to prevent laziness
   - **Example**: Grep for `asyncio.gather()` to find TaskGroup opportunities
   - **Language**: "Recommended: run grep patterns below", "Consider using"

2. **High false positive rate reduces value**
   - Automation finds many candidates, most are legitimate
   - Reviewing all candidates might take longer than targeted manual reading
   - **Example**: Dict literals (many are at legitimate I/O boundaries)
   - **Language**: "Can use automated scan to narrow candidates"

3. **Agent will read code anyway**
   - Pattern requires context that means agent must read code regardless
   - Scan helps prioritize but doesn't prevent lazy skipping
   - **Example**: Naming issues require reading surrounding code for context
   - **Language**: "Optional: use tool for initial candidates"

### When to Make Scans OPTIONAL

Automated scans should be **mentioned as hints only** when:

1. **Doesn't provide concrete candidates to force review**
   - Tool output doesn't give agent specific instances to examine
   - Agent must read code to understand context anyway
   - **Example**: Short variable names (need context to judge if vague)
   - **Language**: "Automation provides hints only; manual review required"

2. **Subjective judgment required for every instance**
   - No way to batch-review candidates; each requires deep analysis
   - Forcing scan doesn't prevent lazy analysis of candidates
   - **Example**: Is code "too verbose"? Is doc "useless"? (pure judgment)
   - **Language**: "No reliable automated detection; read code manually"

### Template Language

```markdown
## Detection Strategy

**MANDATORY first step**: Run `scan_error_handling.py` to find ALL exception handlers.

- Surfaces concrete candidates - forces you to review each exception handler
- Prevents claiming "looks fine" without examining actual code
- Output gives specific line numbers for every try-except block

**Recommended (but not required)**: Use grep patterns to narrow candidates.

- Helpful for finding common cases faster
- Not essential - you'd likely search for these anyway
- Supplement with manual reading for variations

**Optional hints**: AST tool can flag short variable names.

- Provides hints about where to look but doesn't force comprehensive review
- Manual reading required to understand context
- Use only to help prioritize which files to read first
```

### Audit Current Prompts

**Current state** (as of 2025): Most scan prompts treat all automation as optional/recommended. This should be updated:

- ✅ **Keep optional**: Patterns where scan doesn't prevent lazy analysis (naming, verbosity judgment)
- ⚠️ **Should be mandatory**: Scans that surface concrete candidates to force review (error handling, comments, dataclass candidates)
- ⚠️ **Should be recommended**: Scans that save time but agent would search anyway (grep patterns for specific APIs)

**Key criterion**: Does making this scan mandatory force the agent to consider specific candidates it might otherwise skip?

## Scan Prompt Structure

Every scan prompt should follow this structure:

### 1. Pattern Description

Clear examples of BAD and GOOD code with explanations of why.

### 2. Detection Strategy

**Template**:

```markdown
## Detection Strategy

**Goal**: Find ALL instances (100% recall).

**Recall/Precision**: [Characterize the automated tools]

- Tool X has ~100% recall, ~Y% precision
- Tool Z has ~A% recall, ~B% precision

**[MANDATORY/RECOMMENDED/OPTIONAL] approach**:

1. [MANDATORY first step IF high recall]: Run `scan_*.py` to find ALL instances
2. [Verification strategy based on precision]:
   - High precision: Light verification
   - Low precision: Manual review or LLM filtering
3. Fix confirmed issues
4. [If low recall]: Supplement with manual reading of [specific areas]
```

**Include**:

- **Mandatory/Recommended/Optional designation** for each automated tool based on recall
- Specific useful tools (grep patterns, AST checks, linters, analyzers)
- Characterization of each tool's recall/precision for this pattern
- Clear verification strategy based on precision
- Acknowledgment if pattern requires manual reading (low recall)

**Avoid**:

- Full AST implementation code (high-level description is enough)
- Hardcoded lists of specific values to search for
- Claiming automation is sufficient without verification
- Suggesting automated fixes for low-precision patterns without review
- **Making high-recall scans optional when they should be mandatory**

### 3. Examples with Context

Show real examples with enough context to understand the fix.

## General vs. Specific

### ❌ BAD: Hardcoded Specific Cases

```bash
# Find these exact 47 function names that might be useless
rg "get_user_id|fetch_data|load_config|..."
```

**Problem**: Won't generalize to new code, misses different patterns

### ✅ GOOD: General Strategy

```markdown
Find functions where:

- Name is nearly identical to what it returns
- Single statement that just calls another function
- No validation, transformation, or error handling

Use grep to find single-statement functions as candidates, then manually review each.
```

**Benefit**: Agent/LLM can apply this strategy to any codebase

## Prioritize Recall: Don't Miss Issues

**Philosophy**: The goal is 100% recall. Use tools strategically to achieve it.

### ❌ Low Recall Approach (Misses Issues)

```
Only check files modified in last commit
Only look for exact pattern "cast(dict[str, Any], ...)"
Stop after finding 5 issues
```

**Result**: False confidence, missed issues

### ✅ High Recall Approach (Finds Everything)

```
Use grep/AST to find ALL cast() calls (100% recall for this pattern)
Verify each candidate (handle precision issue)
For low-recall patterns: supplement with manual reading
Continue until confident you've found everything
```

**Result**: Comprehensive cleanup, no missed issues

## LLM/Agent Usage

When using LLM agents to apply scan prompts:

### 1. Tool Selection Phase

1. **Understand pattern characteristics**: Does this pattern have high/low recall? High/low precision?
2. **Choose appropriate tools**: grep/AST/ruff/vulture/etc. based on pattern
3. **Set expectations**: Know what each tool will find/miss

### 2. Discovery Phase

1. **Run automated scans**: Use grep/AST/linters to gather ALL candidates (aim for 100% recall where possible)
2. **Understand tool output**: Did tool likely find everything? Or just hints?
3. **Provide context**: Fetch surrounding code for each candidate

### 3. Verification Phase

1. **Match verification to precision**:
   - High precision tool: Light verification ("does this actually match pattern?")
   - Low precision tool: Deep verification ("is this actually problematic?")
2. **Check false positives**: Is this actually bad code or acceptable?
3. **Understand intent**: Why was it written this way? Migration? Workaround?

### 4. Fix Phase

1. **Fix confirmed issues**: Only fix what verification confirmed
2. **Preserve intent**: If code exists for a reason, document instead of delete
3. **Consider supplemental reading**: For low-recall patterns, manually check areas tools might miss

## Helper Scripts: High-Level Descriptions

Instead of providing full implementations, give high-level descriptions:

```markdown
**AST-Based Discovery** (optional):

Build a tool that:

- Walks FunctionDef nodes
- Extracts signatures, types, docstrings
- Flags where docstring just repeats parameter names
- Reports candidates for manual review

Strong coding LLM can reconstruct from this description.
```

**Benefits**:

- Prompts stay concise
- Agents can implement in whatever language/tool makes sense
- Avoids maintenance of sample code
- Emphasizes that implementation is a means, not the goal

## Example: Code Skeleton Generation

**Instead of**: Full working script to strip function bodies

**Provide**: High-level description of what to build:

```markdown
Create helper that strips function bodies, preserving signatures and docs:

1. Parse file with AST
2. For each function, extract signature + docstring
3. Output minimal skeleton (sig + docstring + ...)
4. Feed skeleton to LLM for review

LLM sees context without implementation noise.
```

## Anti-Patterns in Scan Prompts

### ❌ Don't Do This

1. **Ignore tool characteristics**: "Just run this tool" (without explaining recall/precision)
2. **Provide full implementations**: 100+ lines of AST walker code in the prompt
3. **Hardcode specific values**: List of 50 function names to search for
4. **Skip verification for low-precision tools**: "Auto-fix all grep results"
5. **Claim completeness without justification**: "If grep finds nothing, you're done" (when grep has low recall)

### ✅ Do This Instead

1. **Characterize tool reliability**: "grep for cast() has ~100% recall, ~95% precision"
2. **High-level descriptions**: "Build AST tool that finds X pattern"
3. **General strategies**: "Look for functions where name matches return"
4. **Match verification to precision**: "High precision: light verification. Low precision: deep review"
5. **Acknowledge recall limitations**: "This pattern has ~60% recall; supplement with manual reading of [areas]"

## Summary

**Core Loop**:

1. Understand pattern characteristics (recall/precision of available tools)
2. Run automated scans to gather candidates (aim for 100% recall)
3. Verify candidates with appropriate scrutiny (based on precision)
4. Fix confirmed issues
5. For low-recall patterns: supplement with manual reading

**Philosophy**:

- **Goal is 100% recall** - don't miss any real issues
- **Automated tools are essential** - use them strategically based on their characteristics
- **Some patterns have 100% recall** - grep/AST can find all instances
- **Agent uses intelligence** - combines tools, understands their limitations, verifies appropriately
- **Verification matches precision** - high precision = light verification, low precision = deep review

**Tool Characteristics Matter**:

- High recall, high precision: Run tool, verify all → Done
- High recall, low precision: Run tool, filter false positives → Done
- Low recall: Run tool, verify candidates → Supplement with manual reading
- Manual only: Use automation for hints, but expect to find issues by reading

**Result**:

- Comprehensive cleanup (100% recall goal)
- No missed issues (proper tool selection)
- No broken code (appropriate verification)
- Understanding of codebase (verification requires context)
