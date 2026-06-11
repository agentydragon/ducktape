# Anthropic Prompt Engineering Best Practices

## Overview

Anthropic has published extensive guidance on effective prompt engineering for Claude models, emphasizing structured prompts, explicit instructions, and context engineering. These principles generalize well to other LLMs.

## Core Principles

### 1. Be Clear and Direct

**Principle:** Explicitly state what you want the model to do. Don't rely on the model to infer intent from vague instructions.

**Good:**

```
Analyze this code for dead code (unused imports, unreachable code, unused variables).
List each finding with file path and line number.
```

**Bad:**

```
Take a look at this code and see if anything seems off.
```

**Why it matters:** Models perform better when the task is precisely defined. Ambiguity leads to inconsistent outputs and requires the model to guess intent.

### 2. Use Structured Prompts

**Principle:** Organize prompts with clear sections, headers, and delimiters. Use XML tags, Markdown headers, or other structure to separate concerns.

**Pattern:**

```markdown
# Task Description

[What the agent should do]

## Context

[Background information]

## Input Format

[What data the agent receives]

## Required Steps

[Explicit analysis sequence]

## Output Format

[Expected structure and content]
```

**Benefits:**

- Easier for the model to parse and understand
- Clear separation between instructions, context, and examples
- Simpler to debug and iterate (modify one section without affecting others)

### 3. Provide Context Strategically

**Principle:** Give the model relevant context, but avoid overwhelming it with unnecessary information.

**Context types:**

- **Task context:** Why this task matters, what success looks like
- **Domain context:** Specialized knowledge the model might lack
- **Constraint context:** What NOT to do, edge cases to handle

**Anti-pattern:** Dumping entire documentation into the prompt
**Better approach:** Summarize key concepts, link to details if needed

### 4. Use Examples Effectively

**Principle:** Few-shot examples are powerful, but quality > quantity.

**Good examples:**

- Show diverse cases (not just variations of same pattern)
- Include edge cases and boundary conditions
- Demonstrate the reasoning process (not just inputs/outputs)
- Keep examples concise (3-5 is often enough)

**Anti-pattern:**

- 20+ similar examples that all show the same pattern
- Examples without explanations (model must guess the underlying rule)

### 5. Set Clear Role and Persona

**Principle:** Explicitly define the agent's role and expertise level.

**Effective role-setting:**

```markdown
You are an expert code reviewer with 10 years of experience in Python.
You prioritize semantic issues (correctness, maintainability) over style.
You understand the difference between intentional patterns and mistakes.
```

**Why it matters:** Role-setting activates relevant knowledge and biases the model toward appropriate outputs (e.g., "expert" vs "beginner" affects verbosity and assumptions).

### 6. Specify Output Format Explicitly

**Principle:** Define the exact structure and content of expected outputs.

**Good:**

```markdown
## Output Format

Return a JSON object with this structure:
{
"issues": [
{"type": "dead_code", "file": "foo.py", "line": 42, "description": "..."}
],
"summary": "Found 3 issues: 2 dead imports, 1 unused variable"
}
```

**Why it matters:** Prevents the model from choosing its own format, enables automated parsing, ensures consistent outputs across runs.

### 7. Handle Ambiguity Explicitly

**Principle:** When there are judgment calls, provide decision criteria or defaults.

**Pattern:**

```markdown
When you encounter duplication:

- If in UI components (visual consistency), mark as acceptable
- If in business logic, flag as issue
- If unsure, flag with "UNSURE:" prefix for human review
```

**Why it matters:** Reduces variance, makes outputs more deterministic, helps the model handle edge cases without guessing.

### 8. Use Chain-of-Thought for Complex Reasoning

**Principle:** For multi-step reasoning, explicitly instruct the model to show its work.

**Pattern:**

```markdown
Before flagging an issue, explain your reasoning:

1. What pattern did you observe?
2. Why is this problematic?
3. What would a fix look like?

Then provide the structured issue report.
```

**Why it matters:** Improves reasoning quality (model catches its own errors), provides interpretability, helps debug when outputs are wrong.

### 9. Iterate Based on Observed Failures

**Principle:** Prompt engineering is empirical. Test on real examples, identify failure modes, iterate.

**Workflow:**

1. Write initial prompt based on principles
2. Test on diverse examples (easy, medium, hard)
3. Analyze failures (what did the model miss? what did it hallucinate?)
4. Add explicit instructions to address failures
5. Repeat until performance converges

**Example iteration:**

- **Observation:** Model flags intentional duplication as issues
- **Diagnosis:** Missing guidance on acceptable duplication patterns
- **Fix:** Add section "When Duplication is Acceptable" with examples
- **Retest:** Verify false positives reduced

### 10. Leverage Model Capabilities, Don't Fight Them

**Principle:** Understand what the model is naturally good at and structure tasks accordingly.

**Models are good at:**

- Pattern matching across large contexts
- Explaining reasoning in natural language
- Following explicit step-by-step instructions

**Models struggle with:**

- Exact line-by-line counting (use tools instead)
- Complex multi-step arithmetic (use calculator tools)
- Maintaining perfect consistency over very long outputs

**Implication:** Design prompts that play to strengths (e.g., "analyze this code and explain issues" not "count the exact number of characters on line 42").

## Claude-Specific Considerations

### Extended Context Windows

Claude models (particularly Claude 3+) support large context windows (100K+ tokens). This enables:

- **Multi-file analysis:** Pass entire codebases in context
- **Long-range dependencies:** Find cross-file patterns without external tools
- **Rich examples:** Include multiple diverse examples without truncation

**Best practice:** Structure long contexts with clear delimiters (XML tags, Markdown sections) so the model can navigate efficiently.

### Reasoning Models (Claude 4+)

Claude 4 models include enhanced reasoning capabilities. When using reasoning-enabled models:

- **Request explicit reasoning:** "Think step-by-step before answering"
- **Provide reasoning budget:** "You have 500 tokens to reason internally before responding"
- **Encourage self-correction:** "Review your reasoning. Are there any gaps or errors?"

### Tool Use (Function Calling)

Claude excels at tool use. When designing agent prompts:

- **List available tools clearly:** Don't assume the model remembers from fine-tuning
- **Explain when to use each tool:** Provide decision criteria
- **Show tool use examples:** Demonstrate correct usage patterns

### Constitutional AI Considerations

Claude is trained with Constitutional AI (harmlessness, helpfulness, honesty). Implications:

- **Avoid adversarial framing:** Don't say "ignore safety guidelines" (triggers refusal)
- **Frame tasks constructively:** "Help me improve this code" not "Find everything wrong"
- **Acknowledge uncertainty:** Claude is trained to say "I'm not sure" when uncertain - don't penalize this

## Relevance to Prompt Optimization

**How these principles apply to our project:**

1. **Structured prompts:** Our rewritten prompt optimizer system prompt uses clear sections (Mission, Strategic Principles, Toolkit, Problem Space)

2. **Explicit instructions:** We removed vague advice ("be creative") and added concrete guidance (test on full-snapshot train before validation)

3. **Context engineering:** We provide strategic context (two-distribution problem, why baseline is low) without overwhelming with API documentation

4. **Expert role-setting:** We frame the optimizer as an "expert prompt engineer" not a novice needing hand-holding

5. **Output expectations:** We clarify the optimizer should produce prompt text files and use specific tools (upsert_prompt, run_critic_on_example)

6. **Iterative refinement:** We emphasize data-driven iteration (analyze failures → diagnose → iterate) rather than fixed plans

7. **Leverage capabilities:** We encourage the optimizer to write custom analysis scripts (playing to Python/SQL strengths) rather than trying to do everything via tool calls

**Key takeaway:** Effective prompts are clear, structured, context-rich, and empirically refined. They assume appropriate expertise level and provide frameworks rather than step-by-step procedures.

## References

- **Anthropic Prompt Engineering Guide**: <https://docs.anthropic.com/claude/docs/prompt-engineering>
- **Claude 3 Model Card**: <https://www.anthropic.com/claude-3-model-card>
- **Constitutional AI Paper** (Anthropic, 2022): "Constitutional AI: Harmlessness from AI Feedback"
- **Anthropic Prompt Library**: <https://docs.anthropic.com/claude/page/prompts> (curated examples)

## Advanced Patterns

### Prompt Chaining

For complex tasks, chain multiple prompts instead of cramming everything into one:

**Example:**

1. **Analysis prompt:** "Identify all potential issues in this code"
2. **Prioritization prompt:** "Given these issues, rank them by severity"
3. **Explanation prompt:** "For the top 5 issues, explain why they matter"

**Benefits:** Each prompt is simpler, outputs are more focused, easier to debug failures

### Prompt Templates (Jinja, etc.)

Use templating for prompts that need to adapt to different contexts:

```jinja
{% if task_type == "security_review" %}
Focus on: SQL injection, XSS, authentication bypass, secrets in code
{% elif task_type == "performance_review" %}
Focus on: algorithmic complexity, unnecessary allocations, caching opportunities
{% endif %}
```

**Benefits:** DRY (don't repeat yourself), consistent structure, easy to maintain

### Prompt Versioning

Track prompt iterations with version control:

- Save each prompt version with metadata (date, author, intent)
- Link prompts to performance metrics (recall, precision)
- Maintain changelog explaining what changed and why

**Benefits:** Reproducibility, rollback when regressions occur, understand what works

### Human-in-the-Loop Refinement

For critical applications, combine automated prompt optimization with human review:

1. Automated system generates candidate prompts
2. Human reviews for clarity, safety, alignment with goals
3. Human-approved prompts go into production
4. Collect performance data for next iteration

**Benefits:** Catch issues automation misses (bias, safety, subtle incorrectness)

## Common Anti-Patterns to Avoid

### 1. Prompt Sprawl

**Anti-pattern:** 5000-word prompts with every possible instruction and edge case

**Why it's bad:** Overwhelming, hard to maintain, model may miss important parts

**Better approach:** Concise principles + examples, test empirically, iterate based on failures

### 2. Contradictory Instructions

**Anti-pattern:**

```
Be comprehensive and thorough.
Be concise and brief.
```

**Why it's bad:** Model must guess which instruction to prioritize

**Better approach:** Clarify the balance ("Be thorough on semantic issues, brief on style nits")

### 3. Over-Constraining

**Anti-pattern:** Micromanaging every detail, leaving no room for model judgment

**Why it's bad:** Limits model's ability to use its knowledge, brittle to edge cases

**Better approach:** Provide principles and heuristics, trust the model to apply them reasonably

### 4. Assuming Too Much Knowledge

**Anti-pattern:** Using domain jargon without explanation, assuming model knows niche concepts

**Why it's bad:** Model may hallucinate meanings or skip important nuances

**Better approach:** Define key terms, provide domain context

### 5. Ignoring Model Feedback

**Anti-pattern:** Model consistently says "I'm not sure" or "I need more context" - user ignores it

**Why it's bad:** Missing signal about prompt inadequacy

**Better approach:** When model expresses uncertainty, investigate why (ambiguous instructions? missing context?)

## Summary: Principles for Effective Prompts

1. **Clarity:** Be explicit about what you want
2. **Structure:** Organize prompts with clear sections
3. **Context:** Provide relevant background without overwhelming
4. **Examples:** Show diverse, high-quality examples
5. **Role-setting:** Define expertise level and perspective
6. **Output format:** Specify expected structure and content
7. **Decision criteria:** Handle ambiguity explicitly
8. **Reasoning:** Encourage step-by-step thinking for complex tasks
9. **Iteration:** Test empirically, diagnose failures, refine
10. **Play to strengths:** Leverage what models do well, use tools for the rest

**Meta-principle:** Prompt engineering is empirical, not theoretical. Write a reasonable first draft based on principles, then iterate based on observed behavior.
