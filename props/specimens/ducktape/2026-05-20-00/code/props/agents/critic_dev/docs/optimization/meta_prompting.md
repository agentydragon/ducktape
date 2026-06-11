# Meta-Prompting: Conductor Pattern

## Core Concept

A "conductor" LLM orchestrates specialized "expert" LLMs:

- **Conductor:** Breaks down problems, delegates subtasks, synthesizes results
- **Experts:** Domain-specific agents with focused capabilities

## Relevance to Prompt Optimization

You're the **meta-agent** designing expert prompts (the critic). The pattern maps to our workflow:

1. **Decomposition:** Break critic improvement into phases:
   - Diagnostic (what's failing?)
   - Hypothesis (why is it failing?)
   - Refinement (how to fix it?)

2. **Expert perspectives:** Adopt different "lenses":
   - Dead code expert
   - Duplication expert
   - Architecture expert

3. **Iterative refinement:** Like conductor retry logic:
   - Run critic (expert) on examples
   - Grader provides feedback
   - Synthesize into improved prompt
   - Repeat

## When to Use

**Good fit:**

- Complex tasks with clear subtask boundaries
- Need for specialized expertise in different areas
- Latency/cost acceptable for quality improvement

**Poor fit:**

- Simple tasks solvable with single prompt
- Tight latency requirements
- Unclear task decomposition
