# Meta-Prompting: Conductor Pattern for Multi-Expert Systems

## Overview

Meta-prompting is an architectural pattern where a "conductor" LLM orchestrates multiple specialized "expert" LLMs to solve complex tasks. The conductor breaks down problems, delegates subtasks to appropriate experts, and synthesizes results.

## Core Concepts

**Conductor Agent:**

- High-level orchestrator that understands the problem domain
- Routes subtasks to specialized experts based on their capabilities
- Synthesizes expert outputs into coherent solutions
- Maintains conversation context and task state

**Expert Agents:**

- Domain-specific LLMs with specialized knowledge or prompts
- Examples: coding expert, math expert, writing expert, fact-checking expert
- Can be the same underlying model with different system prompts
- Isolated from each other (conductor handles inter-expert communication)

**Key Benefits:**

- **Modularity:** Experts can be updated independently
- **Specialization:** Each expert optimized for specific task types
- **Scalability:** Add new experts without modifying existing ones
- **Interpretability:** Clear delegation and reasoning chains

## Architectural Pattern

### Basic Flow

```
User Query
    ↓
Conductor (analyzes, plans)
    ↓
Expert 1 (subtask A) → Conductor (collects)
Expert 2 (subtask B) → Conductor (collects)
Expert 3 (subtask C) → Conductor (collects)
    ↓
Conductor (synthesizes)
    ↓
Final Response
```

### Conductor Responsibilities

1. **Task Decomposition:**
   - Break complex problems into manageable subtasks
   - Identify which expert(s) should handle each subtask
   - Determine task dependencies and ordering

2. **Expert Selection:**
   - Route subtasks to appropriate experts based on capabilities
   - Handle cases where multiple experts could contribute
   - Retry with different experts if initial attempts fail

3. **Context Management:**
   - Maintain conversation state across expert calls
   - Provide relevant context to each expert
   - Track partial results and dependencies

4. **Result Synthesis:**
   - Combine expert outputs into coherent response
   - Resolve conflicts between expert recommendations
   - Fill gaps that experts didn't address

### Expert Responsibilities

- **Narrow focus:** Solve only the delegated subtask
- **Clear interfaces:** Accept structured inputs, return structured outputs
- **No side effects:** Don't maintain state across invocations
- **Fail gracefully:** Return actionable error messages when unable to solve

## Implementation Patterns

### Pattern 1: Function-Based Experts (OpenAI)

Conductor has access to expert LLMs as "functions" it can call:

```python
# Conductor system prompt (simplified)
You are a conductor agent. You can call these expert functions:
- code_expert(language, task, code) → analysis/suggestions
- math_expert(problem) → solution with steps
- fact_checker(claim, sources) → verification result

Break down user requests, call appropriate experts, synthesize results.
```

Each expert is a separate LLM with specialized prompt:

```python
# code_expert system prompt
You are a coding expert. Analyze code quality, identify bugs, suggest improvements.
Focus only on the provided code snippet. Return structured analysis.
```

### Pattern 2: Multi-Agent Conversation

Conductor facilitates conversation between experts:

```python
# Example: Code review with multiple perspectives
Conductor: "We need to review this Python function."
Security Expert: [analyzes for vulnerabilities]
Performance Expert: [analyzes for efficiency]
Readability Expert: [analyzes for clarity]
Conductor: [synthesizes into unified review]
```

### Pattern 3: Hierarchical Decomposition

Conductor delegates to sub-conductors for complex domains:

```python
Top Conductor
    ↓
Backend Conductor (web architecture, APIs, databases)
    ↓
    Database Expert
    API Expert
    Caching Expert
```

## Comparison with Other Patterns

### vs Single Mega-Prompt

**Meta-prompting advantages:**

- Easier to debug (isolated experts)
- Better specialization (experts don't compete for context)
- Simpler to update (modify one expert without affecting others)

**Single prompt advantages:**

- Lower latency (one LLM call instead of multiple)
- Lower cost (fewer API calls)
- No conductor logic needed

### vs Chain-of-Thought

**Meta-prompting advantages:**

- Explicit specialization (not just reasoning steps)
- Reusable experts across problems
- Conductor can retry with different experts

**Chain-of-thought advantages:**

- Simpler implementation (no orchestration)
- Single coherent reasoning thread
- Lower overhead

## Relevance to Prompt Optimization

**How meta-prompting applies to this project:**

1. **Decomposition guidance:** Teach the prompt optimizer to break down critic improvement into subtasks:
   - Diagnostic phase (what's failing?)
   - Hypothesis phase (why is it failing?)
   - Refinement phase (how to fix it?)

2. **Specialized analysis:** Encourage the optimizer to adopt expert "lenses":
   - Dead code expert perspective
   - Duplication expert perspective
   - Architecture expert perspective

3. **Iterative refinement:** The conductor pattern maps to GEPA's feedback loop:
   - Optimizer (conductor) delegates to critic (expert)
   - Critic runs on examples
   - Grader provides feedback
   - Optimizer synthesizes into improved prompt

**Key takeaway for system prompt:**

- Frame the optimizer as a "meta-agent" that designs expert prompts (the critic)
- Encourage explicit task decomposition (what issue types to cover, in what order)
- Suggest iterative refinement with feedback loops (similar to conductor retry logic)

## References

- **Stanford Meta-Prompting Paper** (2024): "Meta-Prompting: Enhancing Language Models with Task-Agnostic Scaffolding"
- **OpenAI Assistants API**: Multi-agent patterns with function calling
- **LangChain Agent Executor**: Hierarchical agent orchestration
- **AutoGPT/BabyAGI**: Early implementations of autonomous multi-step agents

## Implementation Considerations

**When to use meta-prompting:**

- Complex tasks with clear subtask boundaries
- Need for specialized expertise in different areas
- Want modularity and independent expert evolution
- Latency/cost acceptable for quality improvement

**When NOT to use:**

- Simple tasks solvable with single prompt
- Tight latency requirements
- Cost-sensitive applications (multiple LLM calls)
- Unclear task decomposition (conductor struggles to route)

## Example: OpenAI's Multi-Agent Prompt Optimizer

OpenAI documented a meta-prompting approach to prompt optimization:

**Setup:**

- **Generator agent:** Creates candidate prompts
- **Critic agent:** Reviews prompts for contradictions, clarity issues
- **Refiner agent:** Iteratively improves prompts based on critique
- **Evaluator agent:** Tests prompts on examples, measures performance

**Workflow:**

1. Generator creates initial prompt
2. Critic identifies issues (contradictions, ambiguity)
3. Refiner addresses issues → new prompt
4. Evaluator tests on validation set
5. Loop until performance converges

**Key insight:** Multiple specialized agents outperform single "improve this prompt" agent because each agent focuses on one aspect (generation, critique, refinement, evaluation) rather than trying to do everything.

**Relevance:** Our prompt optimizer agent combines these roles, but the principle holds - break the task into phases (analyze failures, diagnose root causes, propose improvements, test hypotheses).
