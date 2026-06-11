# Prompt Optimization Landscape (Late 2025)

## Summary

This document summarizes research into state-of-the-art prompt optimization frameworks and their applicability to **monolithic prompt optimization with expensive evaluation** (specifically: optimizing a code review critic system prompt using complex codebase examples).

**Key finding**: Most research frameworks assume cheap evaluation and simple examples. For expensive evaluation on complex structured data, simpler agent-based approaches may be more appropriate than sophisticated frameworks.

## Frameworks Evaluated

### 1. GEPA (Databricks/UC Berkeley, 2024)

**Location**: `/code/github.com/gepa-ai/gepa`

**Design**: Evolutionary algorithm with reflection-based mutation and component-level merge

**How it works**:

- Maintains population of programs (Pareto front)
- Two operations:
  - **Reflective Mutation**: Select one program, evaluate on minibatch, reflect on failures, propose improved version
  - **Merge**: Select two programs with common ancestor, combine their independently-evolved components
- Subsample evaluation before full evaluation (cost control)
- Bayesian candidate selection

**Key assumption**: Programs are **compositional** - dict of independent components that can be mixed/matched

**Example use case**:

```python
program = {
    "query_rewriter": "Take user query and...",
    "retrieval_ranker": "Given documents, rank by...",
    "answer_generator": "Synthesize answer from...",
}
```

**Why it doesn't fit our use case**:

- With one component (system prompt), merge degenerates to "pick one or the other"
- No cross-prompt information synthesis (reflective mutation only sees one prompt's failures)
- Designed for multi-stage pipelines, not monolithic prompts
- Still expensive: each iteration evaluates at least one candidate on subsample

**What we'd use from GEPA**:

- Population tracking (Pareto front)
- Subsample-before-full-eval strategy
- Candidate selection heuristics

**Verdict**: Wrong tool for single-prompt optimization. Main benefit (compositional merge) doesn't apply.

---

### 2. DSPy MIPROv2 (Stanford, 2024-2025)

**Framework**: [DSPy](https://github.com/stanfordnlp/dspy)

**Design**: Bayesian optimization over instructions and few-shot examples

**How it works**:

1. **Bootstrap**: Run program many times, collect traces of input/output behavior
2. **Grounded proposal**: Use traces to draft many potential instructions for each module
3. **Discrete search**: Sample mini-batches, propose combinations of instructions + examples, update surrogate model

**Performance**: 13% accuracy improvement on multi-stage programs (Llama-3-8B)

**Key assumption**: Multi-module programs with clear interfaces between stages

**Why it doesn't fit**:

- Same compositional assumption as GEPA
- Designed for DSPy pipelines (chained LM calls)
- Benefits diminish with task complexity (per April 2025 research)

**What we'd use**:

- Bayesian surrogate model approach
- Bootstrapping traces from successful runs

**Verdict**: Overkill for single prompt. Better for systematic pipeline optimization.

---

### 3. PromptWizard (Microsoft Research, January 2025)

**Location**: `/code/github.com/microsoft/PromptWizard`

**Design**: Self-evolving prompts through LLM-driven critique and synthesis

**How it works**:

**Stage 1: Iterative Instruction Optimization**

1. **Mutate**: Generate 10 variations using "thinking styles" (step-by-step, critical thinking, etc.)
2. **Evaluate**: Test each on mini-batches, score by correct answers
3. **Critique**: For poorly performing prompts: "What's wrong?" → Refine. For good prompts: "How to improve?" → Enhance
4. **Select**: Keep top-N, repeat

**Stage 2: Sequential Optimization of Instructions + Examples**

1. Generate/refine few-shot examples (can synthesize from scratch)
2. Alternate: refine instruction OR refine examples
3. Generate CoT reasoning for examples
4. Generate expert identity (system prompt persona)

**Key assumptions**:

- **Cheap evaluation**: Can test 10 candidates × multiple batches × iterations
- **Simple examples**: Q&A pairs that fit in context
- **Self-contained evaluation**: LLM can score its own outputs

**Performance**: Consistently outperformed Instinct, InstructZero, APE, PromptBreeder, EvoPrompt, DSPy, APO, PromptAgent

**Why it doesn't fit our use case**:

1. **Example structure mismatch**:
   - PromptWizard: `{"question": "What is 2+2?", "answer": "4"}`
   - Our case: `{"snapshot": "entire codebase", "true_positives": [58 issues], ...}`
   - **Can't generate synthetic codebases with ground truth**

2. **Evaluation cost mismatch**:
   - PromptWizard: Test 10 prompts × 5 batches = 50 evaluations per iteration (seconds each)
   - Our case: Each evaluation = full critic run (minutes, $1-5, requires Docker/tools)
   - **Can't afford to test 10 variations**

3. **Critique mechanism**:
   - PromptWizard: Paste wrong Q&A pairs into critique prompt
   - Our case: "Wrong example" = entire codebase (can't paste into context)
   - **Can only show failure summaries, not full examples**

**What we'd use**:

- The critique loop pattern (evaluate → critique failures → refine)
- Positive critique for already-good prompts ("what's working well?")
- Expert identity generation

**Verdict**: Best suited for single-prompt optimization among evaluated frameworks, but **fundamentally incompatible** with complex/expensive evaluation. The core innovation (rapid mutation + evaluation cycles) doesn't work when evaluation costs minutes and dollars.

---

### 4. TextGrad (Stanford, Published in Nature 2025)

**Location**: [zou-group/textgrad](https://github.com/zou-group/textgrad)

**Design**: Automatic "differentiation" via text using LLM feedback

**How it works**:

- PyTorch-like API for text optimization
- Backpropagation through text transformations
- Iterative refinement using textual "gradients" (feedback)

**Best for**: Instance-level refinement for hard tasks (coding, scientific Q&A)

**Key assumption**: Can iteratively refine outputs in a test-time loop

**Performance**: State-of-the-art on GPQA (PhD-level Q&A) and LeetCode Hard

**Why it might fit**:

- Designed for continuous text optimization (not just discrete prompts)
- Can handle complex, structured tasks
- Doesn't assume cheap evaluation (focuses on quality over quantity)

**Why it might not fit**:

- Still assumes you can run many iterations
- Gradient feedback requires showing the model its errors (expensive in our case)

**What we'd use**:

- The gradient metaphor (feedback → update direction)
- Focus on quality refinement over rapid exploration

**Verdict**: More appropriate than GEPA/DSPy/PromptWizard for expensive evaluation, but still assumes more iteration budget than we have.

---

### 5. Other Notable Approaches

**EvoPrompt** (ICLR 2024):

- Evolutionary algorithms for discrete prompts
- Up to 25% improvement on BBH benchmark
- Similar assumptions to PromptWizard (cheap evaluation, many candidates)

**Meta's prompt-ops**:

- PDO (Prompt Duel Optimizer) using dueling bandits
- Optimizes prompts for Llama models specifically
- State-of-the-art on BIG-bench Hard and MS MARCO

**GreaTerPrompt** (2025):

- Unified toolkit (text-based + gradient-based)
- Web UI + Python library
- Works with both local and API LLMs

**APE** (Automatic Prompt Engineer):

- Surprisingly competitive despite simplicity
- Doesn't require initial human prompt
- Worth considering as baseline

---

## Our Specific Constraints

### What Makes Our Problem Different

1. **Expensive Evaluation**
   - Each critic run: 2-5 minutes, $1-5 in API costs
   - Requires Docker container, tool execution (rg, ruff, mypy, vulture)
   - 20-50+ LLM calls per run
   - **Can't afford rapid iteration on many candidates**

2. **Complex Structured Examples**
   - Training example = (entire codebase snapshot, targeted files, labeled issues)
   - Can't paste "the example it got wrong" into a prompt
   - Can't generate synthetic training data
   - **Must work with real codebases and ground truth**

3. **Rich Failure Data Available**
   - Grader results: missed TPs, false positives, recall/precision
   - Execution traces: tool calls, reasoning summaries
   - Database of historical runs
   - **Have context that frameworks can't leverage**

4. **Monolithic Prompt**
   - One coherent system prompt (not compositional)
   - No clear decomposition into independent modules
   - GEPA/DSPy compositional benefits don't apply

### What We Have Going For Us

1. **High-quality training data**: Real codebases with expert-labeled issues
2. **Rich evaluation metrics**: Not just accuracy, but recall, precision, TP coverage
3. **Execution transparency**: Full traces in database
4. **Iterative budget**: Can afford ~10-50 expensive evaluations for optimization

---

## Recommendations

### Option 1: Stick with Current Approach (Recommended)

**What you have**:

```python
# run_improvement_agent
1. Select one prompt (best by validation LCB)
2. Provide rich context:
   - Training examples (snapshot slugs + files hashes)
   - Database query examples (how to inspect failures)
   - System overview documentation
3. Agent queries database on-demand:
   - Which TPs were missed?
   - What patterns in execution traces?
   - Which grader runs had low recall?
4. Agent analyzes and writes ONE improved prompt
5. Evaluate on validation set
6. Store results, repeat
```

**Why this is appropriate**:

- **Selective evaluation**: Only one candidate per iteration (affordable)
- **Rich analysis**: Agent can do deep dive into failures (not limited by framework)
- **Flexible**: Can change analysis strategy without rewriting framework
- **Leverages your data**: Uses database/traces that frameworks can't access

**Enhancements to consider**:

1. **Population tracking**:

   ```python
   prompts = [
       (prompt_text, validation_recall, validation_lcb, metadata),
       ...
   ]
   # Keep top-K by different criteria (recall, LCB, precision)
   # Try improving different prompts (not just current best)
   ```

2. **Structured analysis prompts**:
   - Ask agent to follow specific analysis template
   - "First check recall on each issue category, then..."
   - More systematic than free-form analysis

3. **Multi-prompt seed**:
   - Start with 3-5 manually designed prompts (different approaches)
   - Let each evolve independently
   - Track which lineage performs best

4. **Subsample strategy** (from GEPA):
   - Test new prompt on subset of validation examples first
   - Only run full validation if subsample looks promising
   - Saves evaluation budget

---

### Option 2: Adapt TextGrad for Expensive Evaluation

**What to adapt**:

- Use TextGrad's gradient metaphor but with manual feedback
- Each "gradient step" = one expensive critic run + detailed analysis
- Focus on quality of each iteration over quantity

**How it would work**:

```python
for iteration in range(max_iterations):
    # 1. Evaluate current prompt (expensive)
    result = run_critic_on_validation_subset(current_prompt)

    # 2. Generate "textual gradient" (feedback)
    feedback = analyze_failures(result)
    # - "Missed 15 dead code issues: prompt doesn't mention checking for unused imports"
    # - "3 false positives on UI duplication: needs explicit exception for visual consistency"

    # 3. Update prompt using feedback
    new_prompt = textgrad_update(current_prompt, feedback)

    # 4. Accept if improved
    if score(new_prompt) > score(current_prompt):
        current_prompt = new_prompt
```

**Advantages**:

- Structured framework for iterative refinement
- Can plug in your failure analysis as "gradient"
- Good documentation/tooling

**Disadvantages**:

- Still assumes more iterations than you can afford
- Adds framework complexity without clear benefit over simple agent

**Verdict**: Worth trying if simple approach plateaus, but probably overkill initially.

---

### Option 3: Hybrid - Simple Meta-Prompting with Population

**Simplest possible approach** that incorporates research insights:

```python
# 1. Initialize population
prompts = [
    manually_designed_prompt_1,
    manually_designed_prompt_2,
    manually_designed_prompt_3,
]

# 2. Optimization loop
for iteration in range(budget // cost_per_eval):
    # Pick prompt to improve (e.g., highest LCB, or random exploration)
    current = select_candidate(prompts)

    # Get failures on validation subset
    failures = evaluate_and_analyze(current, validation_subset)

    # Ask LLM to improve
    improved = llm.call(
        "Here's a critic prompt and what it missed:\n"
        f"Prompt: {current}\n"
        f"Failures: {failures}\n"
        "Write a better version that addresses these failures."
    )

    # Evaluate improved version
    score_improved = evaluate(improved, validation_set)

    # Add to population if good
    if score_improved >= threshold:
        prompts.append((improved, score_improved))

    # Prune to keep top-K
    prompts = keep_top_k(prompts, k=10)
```

**Why this works**:

- Population gives you exploration (multiple approaches)
- Simple LLM-based improvement (no framework)
- Bounded evaluation cost (budget // cost_per_eval iterations)
- Easy to implement and debug

**Enhancement ideas from frameworks**:

- **Positive critique** (PromptWizard): If score > threshold, ask "what's working well? how to improve further?"
- **Subsample gating** (GEPA): Test on subset before full eval
- **Candidate selection** (GEPA): Smart heuristics for which prompt to improve next
- **Structured feedback** (TextGrad): Template for failure analysis

---

### Option 4: Wait for Better Frameworks

**Reality check**: None of the 2024-2025 frameworks are designed for your problem.

**What would make a framework useful**:

1. **Handles expensive evaluation** (budget-conscious, selective candidates)
2. **Works with complex structured examples** (not just Q&A pairs)
3. **Leverages rich failure data** (grader results, execution traces)
4. **Supports monolithic prompts** (not compositional)

**None exist yet.** The field is focused on:

- Multi-module systems (DSPy, GEPA)
- Cheap evaluation scenarios (PromptWizard, EvoPrompt)
- Simple Q&A benchmarks (most papers)

**Recommendation**: Don't wait. Your current approach is sound, just add population tracking.

---

## Implementation Recommendations

### Phase 1: Enhance Current System (1-2 days)

1. **Add population tracking**:

   ```python
   class PromptPopulation:
       def __init__(self):
           self.prompts = []  # List[(prompt_text, metrics, metadata)]

       def add(self, prompt, validation_result):
           self.prompts.append((prompt, validation_result.recall, validation_result.lcb, ...))
           self.prompts = self.keep_pareto_front(self.prompts)

       def select_for_improvement(self):
           # Strategies: highest LCB, lowest explored, random, etc.
           pass
   ```

2. **Add subsample validation**:

   ```python
   def should_run_full_validation(prompt, subsample_result):
       # Only run expensive full validation if subsample looks promising
       if subsample_result.recall < current_best_recall * 0.9:
           return False  # Not promising, skip
       return True
   ```

3. **Track improvement lineage**:

   ```python
   # Store in database
   ImprovementRun:
       - parent_prompt_sha256
       - child_prompt_sha256
       - rationale (what was changed and why)
       - validation_improvement (delta recall, delta LCB)
   ```

### Phase 2: Systematic Exploration (1 week)

1. **Generate seed prompts**:
   - Ask LLM to generate 5 different approaches to the same task
   - "Write a critic prompt focusing on: (1) dead code, (2) duplication, (3) architecture, (4) testing, (5) comprehensive"
   - Evaluate all, keep best 3 as starting population

2. **Structured improvement prompts**:
   - Template for analyzing failures
   - Template for proposing improvements
   - More systematic than ad-hoc agent reasoning

3. **Metrics dashboard**:
   - Track: which prompts in population, their scores, lineage graph
   - Identify: which approaches work (focus on architecture? focus on tools?)

### Phase 3: Advanced (if needed)

1. **Try TextGrad** if simple approach plateaus
2. **Component decomposition** if patterns emerge:
   - Maybe task framing + tool strategy + reporting format can be separate
   - Then GEPA/DSPy become relevant
3. **Active learning**: Smart selection of which validation examples to test on

---

## Key Takeaways

1. **Your problem is unusual**: Expensive evaluation + complex examples + monolithic prompt. Research frameworks don't target this.

2. **Your current approach is sound**: Agent-based improvement with database access is more appropriate than frameworks designed for rapid iteration.

3. **Low-hanging fruit**: Add population tracking and subsample validation. These are simple enhancements with clear benefits.

4. **Don't force framework fit**: GEPA/DSPy/PromptWizard make assumptions that don't hold for your use case. Using them would mean fighting the framework.

5. **Simple can be better**: A well-instrumented simple approach (LLM improves prompt based on failures) beats a sophisticated framework that doesn't fit.

6. **Your advantage is data**: Rich failure analysis from database is unique. Frameworks can't leverage this. Your agent can.

---

## Further Reading

- [The Prompt Report](https://arxiv.org/abs/2406.06608): Comprehensive survey (58+ techniques, updated Feb 2025)
- [Systematic Survey of Automatic Prompt Optimization](https://arxiv.org/abs/2502.16923) (Feb 2025)
- [Does Automated Prompt Engineering Scale to Complex Tasks?](https://www.tensorzero.com/blog/from-ner-to-agents-does-automated-prompt-engineering-scale-to-complex-tasks/) - TensorZero analysis showing benefits diminish with complexity
- [PromptWizard Microsoft Research Blog](https://www.microsoft.com/en-us/research/blog/promptwizard-the-future-of-prompt-optimization-through-feedback-driven-self-evolving-prompts/)
- [Beyond Prompt Engineering: TextGrad and DSPy](https://medium.com/@adnanmasood/beyond-prompt-engineering-how-llm-optimization-frameworks-like-textgrad-and-dspy-are-building-the-6790d3bf0b34)

---

## Decision Matrix

| Framework              | Cheap Eval? | Simple Examples? | Compositional? | Fits Our Case? | Consider If...                                   |
| ---------------------- | ----------- | ---------------- | -------------- | -------------- | ------------------------------------------------ |
| **GEPA**               | Required    | Any              | Required       | ❌ No          | You decompose prompt into independent components |
| **DSPy MIPROv2**       | Required    | Any              | Required       | ❌ No          | Building multi-stage LLM pipeline                |
| **PromptWizard**       | Required    | Required         | No             | ❌ No          | Have cheap eval + simple Q&A examples            |
| **TextGrad**           | Helpful     | Any              | No             | 🤔 Maybe       | Simple approach plateaus                         |
| **Current Agent**      | No          | No               | No             | ✅ Yes         | This is your baseline (good fit)                 |
| **Agent + Population** | No          | No               | No             | ✅✅ Best      | Easy enhancement with clear benefits             |

---

**Recommendation**: Enhance your current `run_improvement_agent` with population tracking and subsample validation. This gives you the benefits of systematic exploration without the complexity and misfit of research frameworks.
