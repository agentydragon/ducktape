# Further inference migration

Shared judgments now cover native identity, construction, callable summaries
and call-local own-property projections. Remaining work should use concrete
consumers and measured costs.

1. Give compatible cross-module consumers one prepared syntax/value-flow owner.
   Keep native census, author inputs, imported summaries and policy explicit;
   never cache an entire execution index under a source path. Validate reuse
   against different import/author contexts and collect matched CPU/heap profiles.
2. Extend source-composed summaries to ordinary module-loader return values,
   so namespace wrapping can consume their own-property facts. Preserve eager
   loader effects, first-use and partial-cache behavior, captured getters and
   throws. The current wrapper proof covers exact local ordinary inputs; it
   does not establish the shape of an opaque imported loader's result.
3. Extend the scheduler only when that consumer requires multiple producers,
   growing value sets or recursive summaries. Declare each domain's order,
   merge, convergence, completion and work-limit fallback before migrating it.
   Do not equate absence of a positive certificate with proof of absence.
4. Replace duplicate admission/propagation paths as consumers migrate. Require
   real source-to-report tests across every projection, exact assumption unions,
   negative source controls and original/emitted runtime comparison.
5. Use fresh profiles to choose expression arenas, capture-context retention or
   wider bitmap sharing. Check report/output equivalence separately from speed
   and continue using the Claude spec as an integration workload.
