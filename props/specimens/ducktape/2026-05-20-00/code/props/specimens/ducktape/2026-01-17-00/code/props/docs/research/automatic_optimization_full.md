# Automatic Prompt Optimization: APE, OPRO, DSPy, and GEPA

## Overview

Automatic prompt optimization uses LLMs to iteratively improve prompts through generate-test-refine loops. Unlike manual prompt engineering, these systems leverage LLMs' own language understanding to explore the prompt space and discover effective formulations.

## Core Approaches

### APE (Automatic Prompt Engineer)

**Paper:** "Large Language Models Are Human-Level Prompt Engineers" (Zhou et al., ICLR 2023)

**Key idea:** Use an LLM to generate candidate prompts, then select the best one based on evaluation metrics.

**Workflow:**

1. **Generation:** LLM generates N candidate prompts given:
   - Task description
   - Few input-output examples
   - Instruction template (e.g., "Write a prompt that solves this task")

2. **Evaluation:** Test each candidate on held-out evaluation set
   - Run task with each candidate prompt
   - Measure task-specific metric (accuracy, F1, etc.)

3. **Selection:** Choose the prompt with highest evaluation performance

**Strengths:**

- Simple and interpretable
- Discovers prompts humans might not think of
- Works across diverse tasks (classification, generation, reasoning)

**Limitations:**

- No iterative refinement (one-shot generation)
- Requires substantial evaluation budget (N prompts × M examples)
- May not escape local optima (no gradient-based search)

**Relevance to our project:**

- GEPA extends this with iterative refinement (multiple rounds)
- We similarly generate candidates, evaluate on train/valid, select winners

### OPRO (Optimization by Prompting)

**Paper:** "Large Language Models as Optimizers" (Yang et al., Google DeepMind 2023)

**Key idea:** LLMs can optimize prompts through natural language feedback, treating prompt optimization as a meta-learning problem.

**Workflow:**

1. **Initial prompts:** Start with hand-written baseline prompts

2. **Feedback loop:**
   - Evaluate current prompts on training examples
   - Compute performance metrics (accuracy, recall, etc.)
   - Feed performance back to LLM as natural language:

     ```
     Prompt A achieved 75% accuracy.
     Prompt B achieved 82% accuracy.
     Prompt C achieved 68% accuracy.

     Generate a new prompt that improves on these results.
     ```

3. **Iterative refinement:** LLM proposes new prompts based on:
   - What worked well in previous prompts
   - What failed in previous prompts
   - Patterns in successful vs failed prompts

4. **Convergence:** Stop when performance plateaus or budget exhausted

**Strengths:**

- Iterative improvement (escapes local optima)
- Leverages LLM's ability to understand why prompts succeed/fail
- Natural language feedback is interpretable

**Limitations:**

- Can get stuck in local optima (no guaranteed global convergence)
- Requires many evaluation rounds (expensive)
- Success depends on LLM's meta-reasoning ability

**Relevance to our project:**

- Similar feedback loop: evaluate prompt → analyze failures → propose improvement
- We provide richer feedback (execution traces, not just accuracy)
- Natural language critique enables sophisticated reasoning about prompt quality

### DSPy Optimizers (MIPROv2, COPRO, GEPA)

**Framework:** DSPy (Stanford NLP) - modular prompt optimization for LM programs

**Key idea:** Treat prompts as parameters to be optimized, separate prompt composition from prompt optimization.

**Core abstractions:**

- **Signature:** Input/output specification (what the module should do)
- **Module:** LLM call with a prompt (initially generic)
- **Optimizer:** Algorithm that searches for better prompts

**Optimizers:**

#### MIPROv2 (Multi-step Instruction Proposal and Refinement Optimizer)

**Approach:**

1. Generate diverse instruction candidates from examples
2. Score candidates on training set
3. Bootstrap few-shot examples based on model's self-assessment
4. Iteratively refine with ensemble scoring

**Key innovation:** Automated few-shot example selection (not just instruction text)

#### COPRO (Constraint-driven Prompt Optimizer)

**Approach:**

1. Define constraints (e.g., "prompt must be < 200 words", "must mention specific concepts")
2. Generate prompts satisfying constraints
3. Evaluate and refine within constraint space

**Key innovation:** Explicit constraint handling (useful for production systems with requirements)

#### GEPA (Generate, Evolve, Prioritize, Analyze)

**Approach:**

1. **Generate:** Create population of prompt variants
2. **Evaluate:** Test each on training examples
3. **Evolve:** Use reflection LM to analyze failures and propose improvements
4. **Prioritize:** Rank prompts by evaluation metric (e.g., recall, LCB)
5. **Analyze:** Deep-dive into best performers vs worst performers
6. **Repeat:** Iterate until convergence

**Key innovations:**

- **Evolutionary search:** Maintain population, breed winners
- **Reflection-based mutation:** LLM analyzes failures and proposes targeted fixes
- **Statistical rigor:** Use LCB and variance to rank reliably with small sample sizes

**Relevance to our project:**

- **We ARE using GEPA** for prompt optimization
- Our rewritten system prompt guides the "reflection LM" role
- Key insight: Reflection quality determines optimization effectiveness

### Common Patterns Across Approaches

**1. Generate-Evaluate-Refine Loop:**
All systems follow this pattern:

```
while not converged:
    prompts = generate_candidates(feedback)
    scores = evaluate_on_examples(prompts)
    feedback = analyze_performance(scores)
```

**2. Natural Language Feedback:**
Feedback to the LLM is in natural language (not gradients):

- "This prompt missed 20% of issues in file X"
- "Prompt A found dead code but missed duplication"
- "Prompt B had high variance - 30% zero-recall runs"

**3. Train/Valid Split:**

- **Train:** Use for iterative refinement (can inspect results)
- **Valid:** Use for final evaluation (held-out, measures generalization)

**4. Multi-Objective Optimization:**
Often optimize for multiple metrics:

- Primary: task performance (accuracy, recall)
- Secondary: efficiency (latency, cost), reliability (low variance)

**5. Bootstrapping:**
Start from reasonable baseline (not random):

- Hand-written prompts
- Prompts from similar tasks
- Generic "expert" prompts

## Implementation Patterns

### Pattern 1: LLM as Optimizer (OPRO-style)

```python
def optimize_prompt(task, examples, baseline_prompt, budget):
    prompts = [baseline_prompt]
    history = []

    while budget > 0:
        # Evaluate current best
        scores = [evaluate(p, examples) for p in prompts]
        history.append((prompts, scores))

        # Natural language feedback
        feedback = f"""
        Previous prompts and their performance:
        {format_history(history)}

        Generate a new prompt that improves on these results.
        Focus on: {analyze_failures(prompts[-1], examples)}
        """

        # Generate next candidate
        new_prompt = llm_generate(feedback)
        prompts.append(new_prompt)
        budget -= len(examples)

    return max(prompts, key=lambda p: evaluate(p, examples))
```

### Pattern 2: Population-Based Search (GEPA-style)

```python
def optimize_prompt_gepa(task, train_examples, valid_examples,
                         population_size=10, generations=20):
    # Initialize population
    population = [generate_variant(baseline) for _ in range(population_size)]

    for gen in range(generations):
        # Evaluate population on train
        train_scores = [(p, evaluate(p, train_examples)) for p in population]

        # Select top performers
        elite = [p for p, score in sorted(train_scores, key=lambda x: -x[1])[:5]]

        # Reflection: analyze failures
        worst = population[-1]
        failures = diagnose_failures(worst, train_examples)

        # Generate new variants via reflection
        feedback = f"""
        Top prompt achieved {elite[0][1]} recall.
        Worst prompt failed because: {failures}

        Propose improvements to address these failures.
        """
        mutations = [llm_reflect(feedback, e) for e in elite]

        # Next generation: elite + mutations
        population = elite + mutations

    # Final validation
    final_candidates = population[:3]
    valid_scores = [(p, evaluate(p, valid_examples)) for p in final_candidates]
    return max(valid_scores, key=lambda x: x[1])[0]
```

### Pattern 3: Constraint-Driven Search (COPRO-style)

```python
def optimize_with_constraints(task, examples, constraints):
    """
    constraints = {
        "max_length": 500,  # tokens
        "must_mention": ["AST analysis", "type checking"],
        "forbidden_phrases": ["just", "simply"],
    }
    """

    candidates = []
    for _ in range(num_iterations):
        # Generate candidate
        prompt = llm_generate_with_constraints(task, constraints)

        # Validate constraints
        if not satisfies_constraints(prompt, constraints):
            continue

        # Evaluate
        score = evaluate(prompt, examples)
        candidates.append((prompt, score))

    return max(candidates, key=lambda x: x[1])[0]
```

## Best Practices

### 1. Start with Reasonable Baseline

Don't start from scratch. Begin with:

- Hand-written prompt by expert
- Prompt from similar task
- Generic "You are an expert X" prompt

**Why:** Optimization from random initialization wastes budget exploring bad regions

### 2. Use Train/Valid Split Properly

- **Train:** Iterate rapidly, inspect results, diagnose failures
- **Valid:** Test only after train performance looks promising
- **Never:** Optimize on valid (leads to overfitting)

**Red flag:** High train, zero valid → overfit

### 3. Provide Rich Feedback to Reflection LM

Don't just give accuracy numbers. Provide:

- **What failed:** Specific examples the prompt missed
- **Execution traces:** What the model did (tool calls, reasoning)
- **Patterns:** "Missed all duplication issues" not just "82% accuracy"

**Why:** Richer feedback → more targeted improvements

### 4. Measure Variance, Not Just Mean

Use robust metrics:

- **LCB (Lower Confidence Bound):** mean - σ/√n
- **Zero-recall percentage:** How often does prompt fail completely?
- **Max performance:** Best-case scenario (ceiling)

**Why:** A prompt with mean=90%, variance=30% is worse than mean=85%, variance=5%

### 5. Budget Allocation

- **Exploration phase:** Many prompts × few examples (broad search)
- **Exploitation phase:** Few prompts × many examples (deep evaluation)

**Example:**

- Phase 1: 20 prompts × 10 examples = 200 evals (find good regions)
- Phase 2: 5 prompts × 50 examples = 250 evals (refine winners)
- Phase 3: 2 prompts × 100 examples = 200 evals (validate)

### 6. Iterative Refinement > One-Shot Generation

OPRO-style iterative refinement outperforms APE-style one-shot generation:

- Escapes local optima
- Leverages learned patterns from previous iterations
- More sample-efficient (focuses budget on promising directions)

**Exception:** Very tight budget → one-shot may be necessary

## Relevance to Our Prompt Optimizer

### What We're Doing (GEPA)

**Our system:**

- **Population:** Multiple prompt variants evolved over generations
- **Evaluation:** Test on train examples (per-file and full-snapshot)
- **Reflection:** Our prompt optimizer agent analyzes failures, proposes improvements
- **Selection:** Rank by validation LCB, evolve winners

**Our rewritten system prompt:**

- Positions the optimizer as the "reflection LM" in GEPA
- Emphasizes data-driven iteration (not fixed plans)
- Provides strategic principles (not step-by-step procedures)
- Trusts the optimizer to form hypotheses and test them

### Key Design Choices

**1. Baseline-driven optimization:**

- Always compare to current best validation recall
- Any improvement → new baseline
- Goal is continuous improvement (not absolute threshold)

**2. Two-distribution problem:**

- Train examples are mixed difficulty (single-file, multi-file, full-snapshot)
- Valid examples are ONLY full-snapshot (hardest)
- Must test on full-snapshot train before validation (proxy metric)

**3. Rich diagnostic feedback:**

- Execution traces from `events` table
- Tool call sequences, file reads, where critic got stuck
- More informative than just "82% recall"

**4. Statistical rigor:**

- Small validation set → high variance
- Use LCB to rank prompts (penalizes variance)
- Don't trust point estimates with n < 5

**5. Custom scripting encouraged:**

- Write analysis scripts in `/workspace/`
- Form hypotheses, test via custom queries
- Not just "run provided scripts" (explore autonomously)

## Comparison: Automatic vs Manual Optimization

### When to Use Automatic Optimization

**Good fit:**

- Large prompt search space (many possible formulations)
- Clear evaluation metric (accuracy, recall, F1)
- Sufficient budget (100+ evaluation runs)
- Objective is well-defined (not exploratory research)

**Examples:** Classification tasks, structured extraction, code analysis

### When Manual Optimization is Better

**Good fit:**

- Small search space (few obvious approaches)
- Evaluation metric is fuzzy (human preference, aesthetics)
- Very tight budget (<20 evaluation runs)
- Task requires deep domain expertise (automatic system lacks context)

**Examples:** Creative writing, domain-specific reasoning, safety-critical applications

### Hybrid Approach (Best of Both)

**Recommended pattern:**

1. **Manual initialization:** Expert writes baseline prompt
2. **Automatic refinement:** GEPA/OPRO optimizes variations
3. **Human review:** Expert reviews top candidates, picks final
4. **Continuous learning:** Collect performance data, periodically re-optimize

**Why it works:** Human provides domain knowledge, automation provides scale and exploration

## Research Directions

### 1. Multi-Objective Optimization

Optimize for multiple goals simultaneously:

- **Primary:** Task performance (recall, F1)
- **Secondary:** Efficiency (cost, latency)
- **Tertiary:** Interpretability (reasoning transparency)

**Challenge:** Trade-offs (e.g., higher recall often costs more)

### 2. Transfer Learning for Prompts

Can prompts optimized for one task transfer to similar tasks?

**Example:** Prompt optimized for Python code review → Java code review

**Early results:** Partial transfer (task-specific parts must be rewritten, methodology transfers)

### 3. Prompt Compression

Automatically compress verbose prompts while preserving performance:

- Remove redundant instructions
- Consolidate similar examples
- Replace long explanations with concise principles

**Benefit:** Lower cost (fewer tokens), faster inference

### 4. Adversarial Prompt Optimization

Optimize prompts to be robust against input variations:

- Test on adversarial examples (edge cases, ambiguous inputs)
- Penalize prompts that fail on perturbations
- Ensure consistent behavior across input variations

**Relevance:** Production systems need reliability, not just average-case performance

### 5. Neurosymbolic Prompt Optimization

Combine neural (LLM) and symbolic (formal rules) approaches:

- LLM generates candidate prompts (neural)
- Formal verifier checks constraints (symbolic)
- Only constraint-satisfying prompts are evaluated

**Benefit:** Guarantees on prompt properties (safety, compliance)

## References

### Papers

- **APE:** Zhou et al., "Large Language Models Are Human-Level Prompt Engineers" (ICLR 2023)
- **OPRO:** Yang et al., "Large Language Models as Optimizers" (Google DeepMind 2023)
- **DSPy:** Khattab et al., "DSPy: Compiling Declarative Language Model Calls into Self-Improving Pipelines" (Stanford NLP 2023)
- **MIPROv2:** Opsahl-Ong et al., "Optimizing Instructions and Demonstrations for Multi-Stage Language Model Programs" (Stanford NLP 2024)
- **PromptBreeder:** Fernando et al., "Promptbreeder: Self-Referential Self-Improvement Via Prompt Evolution" (Google DeepMind 2023)

### Frameworks

- **DSPy:** <https://github.com/stanfordnlp/dspy> - Modular prompt optimization
- **Guidance:** <https://github.com/guidance-ai/guidance> - Constrained generation for prompts
- **LangChain:** <https://python.langchain.com/docs/modules/prompts/> - Prompt templates and chains
- **HELM:** <https://crfm.stanford.edu/helm/> - Holistic evaluation of language models (includes prompt optimization benchmarks)

## Summary: Key Takeaways

1. **Automatic optimization works:** LLMs can improve prompts through iterative refinement
2. **Natural language feedback is key:** Rich, interpretable feedback enables targeted improvements
3. **Iterative > one-shot:** OPRO/GEPA-style loops outperform APE-style single-generation
4. **Train/valid split essential:** Prevent overfitting by testing on held-out data
5. **Statistical rigor matters:** Use LCB, watch variance, require sufficient sample size
6. **Hybrid approach best:** Combine human domain knowledge with automated exploration
7. **Our project uses GEPA:** Evolutionary search with reflection-based mutation

**Meta-lesson:** Prompt optimization is itself an optimization problem. Use the right tools (evolutionary search, reflection LMs, statistical rigor) to explore the prompt space efficiently.
