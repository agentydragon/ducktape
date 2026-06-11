# Anthropic Prompt Engineering Best Practices

## Core Principles

### 1. Be Clear and Direct

Explicitly state what you want. Don't rely on the model to infer intent.

### 2. Use Structured Prompts

Organize with clear sections, headers, delimiters (XML tags, Markdown). Separate instructions, context, and examples.

### 3. Context Engineering

Give relevant context without overwhelming. Summarize key concepts, reference docs for details.

### 4. Effective Examples

Quality > quantity. Show diverse cases including edge cases. Demonstrate reasoning, not just inputs/outputs. 3-5 examples is often enough.

### 5. Role-Setting

Define expertise level and perspective explicitly.

### 6. Output Format

Specify exact structure. Prevents model from choosing own format, enables automated parsing.

### 7. Handle Ambiguity

Provide decision criteria for judgment calls. Reduces variance, makes outputs deterministic.

### 8. Chain-of-Thought

For complex reasoning, instruct model to show its work. Improves reasoning quality and interpretability.

### 9. Iterate Empirically

Test on real examples, identify failure modes, add instructions to address them.

### 10. Leverage Strengths

Models excel at: pattern matching, explaining reasoning, following step-by-step instructions.
Models struggle with: exact counting, complex arithmetic (use tools), very long consistent outputs.

## Claude-Specific

- **Extended context:** Structure long contexts with clear delimiters (XML tags, Markdown sections)
- **Tool use:** List available tools clearly, explain when to use each, show usage examples
- **Constitutional AI:** Don't say "ignore safety". Frame constructively ("help improve" not "find everything wrong")

## Anti-Patterns to Avoid

1. **Prompt sprawl:** 5000-word prompts with every edge case → hard to maintain, model may miss important parts
2. **Contradictory instructions:** "Be thorough" + "Be brief" → model must guess priority
3. **Over-constraining:** Micromanaging every detail → limits model judgment, brittle to edge cases
4. **Assuming knowledge:** Using jargon without explanation → model may hallucinate meanings

## Summary

1. Clarity: Be explicit
2. Structure: Clear sections
3. Context: Relevant, not overwhelming
4. Examples: Diverse, high-quality
5. Role: Define expertise
6. Output: Specify format
7. Ambiguity: Provide decision criteria
8. Reasoning: Encourage step-by-step
9. Iteration: Test and refine
10. Strengths: Use tools for what models struggle with
