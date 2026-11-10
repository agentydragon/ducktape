# Gaffer Ducktape pin automation

Gaffer currently builds the debundler from its pinned Ducktape source and pins
CI tooling separately. Its `DUCKTAPE_PINNING.md` owns the repin procedure and
validation gates. Automating Gaffer's source repins remains open.

Add a Gaffer workflow backed by a locally runnable, fixture-tested script that
proposes a reviewed source pin update, refreshes locks as needed, and runs the
Gaffer repin gates. Keep runner-tooling pins independent unless intentionally updating them.
Start with manual review; consider auto-merge only after successful cycles and
only for the expected pin and lock files.

Ducktape also publishes a debundle binary. If Gaffer later chooses to consume it,
the automation must keep that binary aligned with the Starlark and source revision
that produced it. This is a separate consumer decision, not a prerequisite for
source-pin automation.
