# Inert class owner coverage reproduction

The report-only effect validator compares the emitter's actual ESM statement
order. A first neutral CLI fixture put an independent `const marker = 1;`
declaration and `class WorkQueue {}` in separate extracted modules with module
names intended to reverse their order. That still produced `swapped_pairs: 0`:
module path order alone did not establish an emitted inversion.

The public CLI fixture now keeps `marker` residual in the entry and extracts
only `WorkQueue`. It checks that the generated entry imports the class module
before the marker statement and that the report counts one swapped pair. The
class owner then has no unknown global, heap, or host coverage; the unrelated
`console.log` owner remains unknown. The opposite control puts
`const earlier = WorkQueue;` before the class and extracts only the class. The
report shows an observed conflict between the class binding write and the
residual TDZ read; only the class endpoint is covered, and the legacy gate
rejects the reorder.

These fixtures distinguish coverage of inert class declaration evaluation
from the effects of later construction. A separate `new WorkQueue(...)`
control executes an instance field initializer and constructor write, preserves
the runtime output, and leaves that constructor-invocation owner uncovered.
