---
name: terminology-drift
description: Audit software systems for terminology drift across code, APIs, data models, and docs. Use for inconsistent vocabulary or unclear domain boundaries; not general documentation cleanup.
---

# Terminology and Concept Drift Audit

Find vocabulary differences that make it unclear whether parts of a system describe the same concept, distinct concepts, or an undocumented relationship. Similar names are leads for investigation, not evidence of equivalence.

## Audit

1. **Establish the system and its contexts.** Read the relevant instructions, current contracts, schemas, and design records. Identify the requested scope and the apparent source of truth for important concepts. Preserve meaningful differences between product areas or bounded contexts when their contracts justify them.
2. **Trace candidate terms.** Search for important terms and likely variants across definitions and uses. Follow representative concepts through types, state and lifecycle rules, storage, API fields, events, configuration, producers, consumers, and documentation. Treat generated artifacts as evidence; find their source when possible.
3. **Compare meanings, not spellings.** For each candidate, compare identity, responsibilities, invariants, lifecycle, and behavior. Classify it as one of:
   - **Synonym drift:** different terms appear to name the same concept.
   - **Semantic overload:** one term appears to name materially different concepts.
   - **Unclear boundary:** concepts overlap, but the rule distinguishing them is missing or applied inconsistently.
   - **Context-specific vocabulary:** terms differ because contexts have distinct contracts, often with a translation at their boundary. This is not a defect by itself.
4. **Verify before reporting.** Use exact definitions, call sites, schemas, tests, or observable behavior as appropriate. Include counterevidence and look for intentional aliases, deprecations, historical names, or public compatibility constraints. Similar names or a single isolated use are not enough to confirm drift.
5. **Recommend options.** For each confirmed issue, explain viable resolutions and tradeoffs. Options may include standardizing a term within a context, making a real distinction explicit in types or contracts, or documenting and enforcing a translation between contexts. Note affected API, persisted, or event boundaries. Keep unresolved intent as an owner question rather than choosing silently.

## Findings

Start with the inspected scope and any important visibility limits. Separate confirmed findings from unresolved candidates. Give each finding a stable ID and include:

- Terms and concepts involved, with a short classification.
- Evidence for how each term is defined and used, with exact paths and line references where available.
- Where the meanings align or diverge, including relevant counterevidence.
- Practical impact and confidence.
- Resolution options with tradeoffs, or the specific question that needs an owner’s decision.

Prioritize confusion at system boundaries, public contracts, persistence, and widely reused concepts over cosmetic naming differences. Keep the report concise enough to review, but trace each claim to evidence.

This is an audit skill: do not edit files or rename concepts unless the user explicitly asks to implement a selected resolution. Do not claim a runtime behavior was verified when only source or documentation was inspected.
