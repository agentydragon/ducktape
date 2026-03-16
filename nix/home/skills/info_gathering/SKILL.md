---
name: info-gathering
description: >
  Optimal information gathering under uncertainty. Use this skill whenever you need to
  reduce uncertainty about something — whether to learn a true value, make a decision,
  design an experiment, elicit user preferences, prioritize research, or plan any
  investigation where you don't know enough yet and want to learn efficiently. Trigger
  whenever: the user asks "what should I find out", "help me figure out", "what questions
  should I ask", "help me research", "design an experiment", "interview me about my
  preferences", "what do I need to know to decide", or any situation where Claude
  recognizes it has meaningful uncertainty that could be reduced by gathering information
  and wants to do so optimally. Also trigger when the user wants to compare options
  (housing, jobs, investments, algorithms, products) and the comparison requires obtaining
  information that isn't yet available. Even if the user doesn't explicitly frame it as
  "information gathering," use this skill when the core bottleneck is uncertainty that
  could be resolved by asking questions, running searches, consulting data, or running
  experiments.
---

# Optimal Information Gathering

This skill implements a disciplined framework for reducing uncertainty efficiently. It
applies whenever you face uncertainty about quantities, states, or preferences that
matter — either because knowing the truth is the goal, or because a downstream decision
depends on it.

The skill produces and maintains a **living epistemic state document** — an auditable,
self-contained record of everything known, how it was learned, what's still uncertain,
and what the optimal next information-gathering actions are. Another Claude instance
reading only this document (and any attached artifacts) should be able to independently
verify every step.

## Core loop

```
1. FRAME      → Define what's uncertain, why it matters, what "done" looks like
2. ENUMERATE  → Survey the full action space: what levers do you have? (see below)
3. GROUND     → Establish priors from real data, reference classes, empirical distributions
4. CHALLENGE  → Generate alternative hypotheses, apply debiasing (see below)
5. PLAN       → Identify candidate info-gathering actions, estimate their cost and
                 expected information value, select the best next action(s)
6. EXECUTE    → Carry out the action (ask question, run search, run experiment, delegate)
7. INTEGRATE  → Record the result verbatim, update posteriors, recompute the full
                 decision landscape as if invoking the skill fresh from the new state
8. CHALLENGE  → Again: generate scenarios where your current top hypothesis is wrong
9. REPEAT     → Until stopping criteria met (sufficient certainty, diminishing VOI,
                 budget exhausted, user satisfied)
```

### ENUMERATE: Surveying the action space

Before planning, explicitly inventory what you can actually do. Don't default to
the obvious (ask the user questions). Enumerate concretely:

**Direct levers** (things you can do right now, no user involvement):

- Web search, web fetch, read uploaded files
- Run code, compute, simulate
- Search internal tools (Drive, email, calendar, Slack — whatever's connected)
- Call APIs you have access to
- Generate artifacts, run analysis

**User-mediated levers** (things the user can do that you can't):

- Answer questions (cognitive)
- Provide existing data (files, ratings, spreadsheets, photos)
- Take physical measurements or check physical state
- Try behavioral interventions and report back
- Grant you access to new tools/data/APIs
- Run experiments in the physical world

**Acquirable levers** (things neither of you can do yet, but could enable):

- "Could you share screen / upload a photo / give me access to X?"
- "Is there a dataset we could buy/download?"
- "Could you connect your [service] so I can search it?"

Spend 30 seconds at the start of each new domain creatively brainstorming: what's
the weirdest, most efficient action that could resolve a lot of uncertainty at once?
The best info-gathering strategies often come from noticing a lever that isn't
obvious. "Do you have a Letterboxd account?" "Can you take a photo of the room?"
"Is there a public API for this data?" "Can you paste your browser history?"

### CHALLENGE: Debiasing and calibration

LLMs (including you) have a strong tendency to anchor on the first plausible
hypothesis and then confirmation-bias through subsequent evidence. This step
exists to counteract that.

**At initial hypothesis formation** (step 4, first pass):
After forming your initial best guess, explicitly generate 3-5 alternative
scenarios that are consistent with the same evidence but lead to different
conclusions. For each, assign a minimum credible probability — the lowest
probability you could sanely give it without being reckless. Your top hypothesis
should not start above ~60-70% confidence unless the evidence is overwhelming.

Format for the epistemic state document:

```
Hypothesis check:
H1 (current best): [hypothesis] — P = 0.45
H2: [alternative] — P = 0.25 (because: [reasoning])
H3: [alternative] — P = 0.15 (because: [reasoning])
H4: [alternative] — P = 0.10 (because: [reasoning])
H_other: catch-all — P = 0.05
Key distinguishing test: [what evidence would separate H1 from H2?]
```

**At each update** (step 8):
After integrating new evidence, before moving on, ask:

- "If my current top hypothesis is WRONG, what are the most likely ways it's wrong?"
- "Is there evidence I'm discounting because it doesn't fit my leading theory?"
- "Have I updated enough on this evidence, or am I anchoring on my prior?"
- "Would a skeptical colleague look at my evidence log and agree with my posterior?"

This is not optional decoration — it's a core part of the skill. The failure mode
of "confidently wrong early, then confirming the wrong thing" is the single most
common way information gathering goes badly. The cost of generating alternatives is
near-zero; the cost of anchoring on the wrong hypothesis can waste the entire
information budget.

Every iteration through this loop produces an updated epistemic state document. The
document is the primary artifact of this skill.

---

## The Epistemic State Document

Create and maintain a file called `epistemic_state.md` (or a directory `epistemic_state/`
if PDFs, data files, or other artifacts accumulate). This document is the single source
of truth. It must be **self-contained**: another Claude instance reading it cold must be
able to verify every claim and reproduce every calculation.

### Required sections

```markdown
# Epistemic State: [Project Title]

## Last updated: [ISO timestamp]

## 1. Objective

What we're trying to learn or decide, and why. If decision-theoretic: the decision
space, the utility function (in explicit units), and how information maps to better
decisions.

## 2. Available Action Space

Inventory of all currently available levers (direct, user-mediated, acquirable),
updated as new capabilities are discovered or granted. Include creative/non-obvious
actions identified during ENUMERATE.

## 3. Uncertainty Register

A table of every quantity we're uncertain about. For each:
| ID | Quantity | Prior distribution | Source of prior | Entropy (bits) | Status |
|----|----------|--------------------|-----------------|----------------|--------|
| U1 | ... | ... | ... | ... | open/resolved |

## 4. Hypothesis Space (where applicable)

For diagnosis/identification problems, maintain competing hypotheses:
| ID | Hypothesis | Probability | Key evidence for | Key evidence against | Distinguishing test |
Top hypothesis must not exceed ~60-70% unless evidence is overwhelming.
Include H_other catch-all ≥ 5%.

## 5. Evidence Log

Chronological record of every piece of evidence obtained. For each entry:

- Timestamp
- Action taken (question asked / search run / experiment performed / etc.)
- Action type (human_elicitation | research_lookup | experiment | delegation | observation)
- Cost incurred (in appropriate units — time, $, tokens, cognitive load)
- Raw result (VERBATIM — exact user quote, exact search snippet with URL, exact
  measurement)
- Stable pointer (URL, citation, file hash, "user verbal response in this conversation")
- Likelihood ratio or update applied, with explicit reasoning
- Which uncertainties (by ID) this evidence bears on
- Posterior after update (distribution or point estimate + confidence)
- [VIBE] tag on any component that lacks empirical grounding, with note on how to de-vibe

## 6. Current Posterior State

The current best estimate of each open uncertainty, with:

- Distribution (if continuous) or probability table (if discrete)
- Entropy in bits
- All numbers sourced or tagged [VIBE]

## 7. Action Queue

Ranked list of candidate next actions, each with:

- Description of the action
- Action type
- Estimated cost (in units appropriate to type)
- Expected information gain (bits of entropy reduction) OR expected VOI (in utility units)
- How the gain/VOI estimate was computed (reference class, simulation, analytical)
- [VIBE] tag with de-vibe plan if the estimate is ungrounded
- Net value (gain minus cost, in comparable units if possible)

## 8. Decision Tree (if planning multiple steps)

The adaptive plan: what to do next depends on what we learn. Show the tree at least
2-3 nodes deep where branching matters. Include:

- Branch conditions (what answer or outcome leads where)
- Expected value at each branch
- Why this branching structure (not just a flat list)

## 9. Stopping Criteria

When to stop gathering information:

- Target entropy threshold
- VOI of best remaining action falls below cost
- Budget exhausted
- User declares satisfaction
- Decision quality plateaued

## 10. Vibes Ledger

Consolidated list of all [VIBE]-tagged items in the document, with:

- What was vibed
- Why it couldn't be grounded
- What action would de-vibe it (this is itself a candidate info-gathering action)
- Priority for de-vibing (is this vibe load-bearing for the current best action?)
```

### Formatting rules for the Evidence Log

**User responses**: Include the exact question asked AND the exact verbatim answer:

```
Q: "On a scale of 1-5, how important is walking distance to transit?"
A (verbatim): "Probably a 4, but I'd go to 3 if the place had parking"
Interpretation: Transit importance ~0.7-0.8 weight, but substitutable with parking.
Update: [explain the Bayesian update with explicit numbers]
```

**Research results**: Include URL, date accessed, relevant excerpt (keep it short but
sufficient to justify the update), and ideally a content hash or archive link:

```
Source: BLS Occupational Outlook Handbook, https://www.bls.gov/ooh/..., accessed 2025-03-14
Finding: Median salary for [role] is $X (2024 data, n=Y workers)
Update: Sets U3 prior to LogNormal(μ=log(X), σ=0.3) based on BLS methodology
```

**Experiments**: Protocol, result, sample size, effect size, confidence interval.

---

## Grounding priors: How to not make things up

The central discipline of this skill is that **every number must be grounded or
explicitly flagged as ungrounded**. Here's how to ground things, roughly in order of
preference:

### Tier 1: Empirical data (no [VIBE] tag needed)

- Published statistics with known methodology (BLS, Census, IMDb, SEC filings)
- Reference classes with documented base rates ("X% of Y did Z, source: [link]")
- Your own prior computations from data you can show

### Tier 2: Calibrated estimates from reference classes ([VIBE] tag optional, reasoning required)

- Start from a known base rate, apply explicit likelihood ratios for conditioning
  on specific evidence
- Each likelihood ratio must be justified: "Companies with [feature] are ~2x more
  likely to [outcome] based on [reasoning/data]"
- If the likelihood ratio itself lacks data: tag it [VIBE] and specify what data
  would ground it

### Tier 3: Explicit vibes ([VIBE] tag mandatory)

- When no reference class is available and you must estimate
- State the estimate, your reasoning, your confidence in the estimate itself
- Specify exactly what would de-vibe it
- Flag whether this vibe is load-bearing (does the optimal next action change if
  this vibe is wrong?)

In every domain, actively search for grounding sources (see Domain References table
below). Treat user-stated preferences through a noise model — stated preferences have
~70-85% test-retest reliability per choice modeling literature. Never vibe health/medical
stats — source them.

---

## Information-gathering actions: The heterogeneous action space

The skill operates over multiple action types. Each has different cost structures and
informativeness profiles. The optimal strategy typically interleaves them.

### Action types

| Type                     | Examples                                                                 | Typical cost unit                                | Informativeness characteristics                                                                            |
| ------------------------ | ------------------------------------------------------------------------ | ------------------------------------------------ | ---------------------------------------------------------------------------------------------------------- |
| **Human elicitation**    | Ask user a question, interview expert                                    | Cognitive load, time, social capital             | High per-question if well-designed; subject to noise, inconsistency, social desirability bias              |
| **Research/lookup**      | Web search, database query, read a paper                                 | Tokens, time, $ for paid sources                 | Can be very high if the source exists; zero if it doesn't                                                  |
| **Self-experiment**      | Run code, compute something, simulate                                    | Compute time, tokens                             | Precise, repeatable, but limited to what you can compute                                                   |
| **User physical action** | Measure something, try an intervention, check a physical state           | User time + physical effort (seconds to minutes) | Can be extremely high-IG for cheap cost — the user can interact with physical reality, which Claude cannot |
| **Delegated experiment** | Ask user to run a multi-step test, deploy an A/B test, build a prototype | User time, calendar delay, $                     | High quality but high cost; reserve for high-VOI actions                                                   |
| **Observation**          | Read uploaded files, examine existing data                               | Low (just attention)                             | Variable — sometimes the answer is already in the data                                                     |

### User as physical instrument

The user can interact with physical reality — Claude can't. This makes quick physical
actions and behavioral micro-experiments a distinct, often underrated action type.
Design them like diagnostic tests: optimize sensitivity/specificity for the distinction
you care about.

**Examples**:

| Scenario          | Action                                                           | Why it's good                                             | Cost    |
| ----------------- | ---------------------------------------------------------------- | --------------------------------------------------------- | ------- |
| "I'm dizzy"       | "Try blowing your nose — does it change?"                        | Bifurcates sinus vs. other causes                         | ~10 sec |
| "I'm dizzy"       | "Stand up slowly — does it get worse?"                           | Tests orthostatic component                               | ~15 sec |
| Room optimization | "Pace the wall with the window (1 pace ≈ 2.5 ft)"                | Grounds dimensions from [VIBE] to measured                | ~30 sec |
| Hardware debug    | "Is the router light blinking or solid?"                         | Bifurcates failure mode categories                        | ~5 sec  |
| Weight loss       | "Try setting alarm 30 min earlier for 3 days — do you eat less?" | Tests if schedule change affects eating for _this person_ | 3 days  |
| Sleep quality     | "No screens 30 min before bed tonight — fall asleep faster?"     | Single-variable behavioral experiment                     | 1 night |

Two subcategories:

- **Instant diagnostics** (seconds): check a state, take a measurement. Cheap, precise,
  bifurcating. Offer approximation methods (paces, arm span) when exact tools unavailable.
- **Behavioral micro-experiments** (hours-days): try an intervention, report back. More
  costly but resolves "does X work for this specific person" — the key uncertainty in
  behavior optimization, since individual variation dominates base rates.

Design principles: minimize effort, bifurcate don't just measure ("does X change when
you do Y?" > "describe X"), propose don't prescribe (show VOI so user decides if cost
is acceptable), make interventions minimal/time-bounded/reversible, explain the
diagnostic logic so user cooperates.

### Designing human-facing questions

Cognitive cost ladder (increasing cost, roughly increasing max IG):

1. Yes/No (~1 bit max, ~5 sec)
2. Forced binary choice / pairwise comparison (~1 bit, fast, use Bradley-Terry for
   reconstructing rankings from many pairs)
3. Small-scale rating 1-5 (~2.3 bits max)
4. Ranking 3-4 options (moderate effort, good bits)
5. Scoped open-ended — "What's your budget?" (high bits if they know the answer)
6. Unbounded open-ended — "What matters to you?" (high potential, noisy, use early
   when prior is flat)

Design the first question to split the hypothesis space roughly in half (binary
search on belief space). Always consider: "is there a data dump shortcut?" —
asking "do you have IMDb ratings / a spreadsheet / existing notes?" can yield
100+ bits in one action.

---

## Quantitative reasoning

Use standard decision theory math throughout: Shannon entropy for discrete/differential
entropy for continuous distributions, expected information gain (H(prior) - E[H(posterior|O)]),
VOI (E[max_d U(d,posterior|O)] - max_d E[U(d,prior)]), net value = gain - cost.

When costs and gains are in different units (bits vs. dollars vs. user-seconds), establish
an explicit exchange rate with stated reasoning. Tag the exchange rate [VIBE] if ungrounded.

---

## Adaptive decision tree planning

Plan the information-gathering sequence as a decision tree at least 2-3 levels deep
when answers to early questions determine which later questions are relevant. When
uncertainties are roughly independent, a flat prioritized list is fine.

Notation for the epistemic state document:

```
├─ Ask: "Budget above or below $3000/mo?" [IG: 0.95 bits, cost: low]
│  ├─ IF above → Ask: "Nob Hill vs. Pacific Heights?" [IG: 0.7 bits]
│  └─ IF below → Ask: "Commute time or space more important?" [IG: 0.8 bits]
├─ Search: Zillow median rents by SF neighborhood [IG: 0.6 bits, cost: 1 search]
```

---

## Maintaining the document across turns

After each information-gathering action:

1. **Append** the new evidence to the Evidence Log (never delete old entries)
2. **Update** the Uncertainty Register with new posteriors
3. **Recompute** the Action Queue — re-rank all remaining actions given new state
4. **Update** the Decision Tree if branching has changed
5. **Review** the Vibes Ledger — has new evidence de-vibed anything? Are there new vibes?
6. **Check** stopping criteria — should we stop?
7. **Summarize** the state change briefly for the user: what we learned, how it
   changed the picture, what's recommended next

The updated document should read as if the skill were invoked fresh from the current
state — no need to trace through the full history to understand the current
recommendation (though the history is preserved for auditability).

---

## Interaction Modes

The epistemic state machinery is always running underneath, but the **surface
presentation** adapts to context. Choose the mode that minimizes total cost
(yours + user's) while maximizing information flow. Modes can be mixed within a
session — start with interactive interview, pause to do autonomous research,
come back with a batch questionnaire.

### Mode 1: Interactive interview (one action per turn)

Use when the primary information source is the user and you're eliciting preferences,
constraints, or domain knowledge from them. The user-visible output per turn should be
**lean** — the question(s) to answer, maybe a one-line note on why this question
matters. Don't show the epistemic state machinery unless asked.

What the user sees:

```
Have you seen Inception? If yes, did you like it?
(This helps me calibrate your taste for complex sci-fi — ~0.8 bits)
```

What happens behind the scenes: full epistemic state update, VOI recomputation,
decision tree re-evaluation, next question selection.

**Creative action design is critical here.** Think hard about what actions are
actually cheap AND informative in context. Examples for movie taste elicitation:

| Action                                  | Cost to user           | Expected IG                          | Notes                                                                                                                             |
| --------------------------------------- | ---------------------- | ------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------- |
| "Did you see [popular movie]? Like it?" | ~5 sec                 | High if seen (0.5-1 bit), low if not | Pick movies with high viewership base rate — more likely they've seen it, so higher expected IG                                   |
| "Did you see [niche movie]?"            | ~5 sec                 | High if seen, but P(seen) is low     | Only worth asking if you have reason to think this user watches niche films                                                       |
| "Please watch [movie] and tell me"      | 1.5-3 hours            | Very high if done                    | Absurdly costly — almost never worth it for preference elicitation                                                                |
| "Do you have IMDb/Letterboxd ratings?"  | ~10 sec ask + variable | Potentially massive (100+ bits)      | Creative! If user has rated 200 movies, this one action dominates dozens of individual questions. Always look for these shortcuts |
| "What genres do you like?"              | ~15 sec                | ~2-3 bits                            | Open-ended, cheap, good for early exploration when prior is flat                                                                  |
| "Top 3 favorite movies?"                | ~20 sec                | ~3-5 bits                            | Slightly more cognitive effort but very informative                                                                               |

The general principle: **look for creative high-IG/low-cost actions before
grinding through the obvious question sequence.** "Do you have an existing
data source I could read?" is often the single highest-VOI question you can ask.

### Mode 2: Batch questionnaire

When you have multiple independent uncertainties resolvable cheaply in parallel.
Generate a structured form: yes/no checklists, Likert batteries, pairwise comparison
sets, or multiple choice. Don't batch questions whose answers affect which later
questions to ask. Show expected total time and total IG.

### Mode 3: Autonomous research

When highest-VOI actions are lookups/searches/computations. Chain multiple research
actions per turn, update epistemic state, return to user with concise findings +
next question informed by what you learned. Don't ask the user things you could
look up yourself.

### Mode 4: Experiment design

When resolving key uncertainties requires experiments. Output an optimized protocol:
what to measure (which uncertainties), cost, expected IG/VOI, sample size/power
analysis, and a decision tree over possible outcomes.

### Mode 5: Hybrid / multi-source

The most common mode. Interleave user questions, research, computation, delegation.
Core discipline: **after each action of any type, recompute optimal next action
across ALL action types.** Don't get stuck in one mode when another has higher VOI.

### Mode 6: Fully autonomous (no human in the loop)

Use when the user hands you a goal, resources (API keys, budget, compute, tools),
and says "go." There is no human to ask questions to — you are the entire
information-gathering agent. The user will check back later for results.

This mode demands the most discipline because there's no human correcting you
mid-run. The epistemic state document becomes your primary thinking tool AND
your deliverable.

**Setup phase** (before spending any resources):

1. Parse the objective precisely. What counts as success? What's the utility
   function? If ambiguous, front-load clarification before the user leaves.
2. Inventory resources: budget ($, compute hours, API calls), time constraints,
   available tools/APIs, any uploaded context.
3. Build the initial uncertainty register from domain knowledge and quick
   free/cheap research (web searches, reading provided docs).
4. Lay out the full decision tree at least 3 levels deep. This is your
   experiment plan.
5. Establish budget allocation: don't spend 80% of budget on the first
   experiment. A reasonable default is:
   - ~10-15% on initial orientation (cheap lookups, literature/benchmark review)
   - ~50-60% on the core experiment sequence
   - ~20-30% reserved for follow-up on surprising results or promising leads
   - ~5-10% buffer for unexpected costs
6. Define stopping criteria in terms the user cares about: target performance
   metric, confidence level, budget exhaustion, diminishing returns threshold.

**Execution loop**:

```
while budget > 0 and not stopping_criteria_met:
    1. Review current epistemic state
    2. Enumerate candidate next actions with estimated cost and VOI
    3. Check: is best action's VOI > cost? If not → stop, report findings
    4. Check: does budget allow this action? If not → find cheaper alternative
       or stop
    5. Execute the action
    6. Record result with full provenance in evidence log
    7. Update posteriors
    8. Recompute action queue and decision tree
    9. Log cumulative spend and remaining budget
```

**Critical disciplines**: Track cumulative spend after every action; never exceed
budget. Cheap before expensive (google before computing, read literature before
training). Minimal viable experiments (smallest experiment distinguishing top
hypotheses). Progressive commitment (explore broadly with cheap probes, then exploit).
Track IG/$ over time — stop when it drops steeply. Log failures, don't burn budget
retrying broken things. Checkpoint the epistemic state to disk periodically.

**Deliverables**: Full epistemic state document, findings summary with confidence
levels, budget accounting (spend + retrospective IG/$ per action), recommendations
for continuation, all artifacts organized for reproducibility.

**Example**: "Here's a Modal API key and $200 credits. Figure out whether
fine-tuning a small LLM or using RAG gives better results for our customer
support use case. Here are 500 example tickets."

Setup: Read tickets (free), web search for RAG vs fine-tuning benchmarks on
similar tasks (free), estimate compute costs for both approaches. Decision tree:
if fine-tuning a 7B model for 1 epoch costs ~$15 and RAG indexing + retrieval
eval costs ~$5, start with RAG (cheaper first). If RAG gets >80% accuracy on
a held-out set, fine-tuning may not be worth it (compute VOI calculation). If
RAG <60%, fine-tuning is clearly worth trying. If 60-80%, run a minimal
fine-tuning experiment to compare. Reserve $50 for follow-up experiments on
whichever approach wins.

### Choosing and switching modes

Assess: Is user present? Who has the information? Are unknowns independent (batch)
or sequential (tree)? Is there a resource budget? Always check for creative shortcuts
("do you have existing data I could read?"). Switch modes when optimal next action
changes type — don't announce, just do it.

### Presentation rules

**Interactive modes**: Show only the question/action + brief IG/VOI note. Show
full epistemic state on request. Show VOI/cost analysis for costly actions.

**Autonomous mode**: The epistemic state document IS the deliverable.

---

## Domain-specific grounding references (non-exhaustive)

When entering a new domain, seek out these kinds of sources to ground your priors.
Don't rely on vague impressions — find the data.

| Domain               | Good sources for base rates / distributions              |
| -------------------- | -------------------------------------------------------- |
| Real estate          | Zillow, Redfin, Case-Shiller, local MLS data, Census ACS |
| Movies/entertainment | IMDb, MovieLens dataset, Rotten Tomatoes distributions   |
| Startups/finance     | PitchBook, Crunchbase, SEC EDGAR, Damodaran datasets     |
| Labor/salaries       | BLS OOH, Glassdoor, levels.fyi, H1B salary database      |
| ML benchmarks        | Papers With Code, MLCommons, published benchmark papers  |
| Health/medical       | PubMed, CDC MMWR, Cochrane reviews, FDA labels           |
| Consumer preferences | Academic choice modeling lit, Nielsen, Pew surveys       |
| Software eng         | DORA reports, Stack Overflow surveys, GitHub Octoverse   |

Treat this table as a starting point. The skill should actively search for domain-
appropriate grounding sources when entering a new problem.

---

## Example: Apartment search (abbreviated)

Frame: which apartment to rent. Utility = weighted sum of cost, commute, space,
neighborhood. Weights unknown → uncertainties to resolve.

Ground: U1 (budget) and U2 (commute destination) are unknown, ask user [IG ~3-4
bits each, cost: ~5 sec]. U3 (neighborhood rents) from Zillow [research]. U4
(preference weights) via pairwise comparisons after U1-U3 narrow the space.

Execute: Ask budget + commute in one turn. User says "$2500 ideal, $3000 max" and
"remote but near BART for FiDi." Search Zillow given constraints. Now highest-VOI
action is preference elicitation: "Would you rather have a bigger place 15 min from
BART, or smaller 3 min from BART?" Continue with full evidence logging until user
can decide.

---

## Cautions

- **Don't over-formalize trivial decisions.** If the user just needs a quick
  preference check, don't build a 50-line epistemic state document. Scale the
  formalism to the stakes and complexity.
- **Don't let the framework substitute for judgment.** The numbers are there to
  discipline reasoning, not replace it. If the model says "action A has 0.01 bits
  more IG than B" but A is much more costly, use judgment.
- **Don't hide uncertainty behind math.** A precisely computed expected value from
  vibed inputs is worse than an honest "I don't know, here's what I'd need to find
  out." The [VIBE] discipline exists to prevent this.
- **The document is for auditability, not performance.** Don't optimize for making
  the document look impressive. Optimize for a skeptical reader being able to check
  your work.
