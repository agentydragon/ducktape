# augur/sim

Typed financial declarations, execution preparation, fixed-point helpers and
shared result types. Financial settlement currently executes in <../rust/README.md>;
experiment policies and their outer time loops are Python code.

## Experiment path

An experiment supplies a `Scenario`, market paths, jurisdiction rules and
locations to `compile_run` in <backend.py>. It starts the common `ActionSession`,
submits one batch of ordered actions per decision month, and reads typed results
from <results.py> and books from <books.py>. Sampling, fitting, policy choice and
report definitions belong to the caller.

See <../x/monthly_actions/README.md> for explicit actions and
<../x/joint_spending_allocation/README.md> for a spending/allocation comparison.
Shared proposal helpers live in <../policy/>; they do not settle trades or taxes.

`CompiledRun` currently exposes its lowered execution document. That is a
transport boundary, not a second mutable book. The app and remaining legacy
acceptance/benchmark consumers still use the configured `Engine` contract;
it is not the interface new experiments should extend.

## Outcomes and failure

A rejected action stops that path, preserves successful earlier actions, and
executes no later actions or policy calls on it. Unpaid due claims are a distinct
stop reason. Invalid routing/input and simulator bugs are errors, not modeled
investment outcomes. Independent paths continue.

Results preserve original rollout IDs, the observed prefix, ending books and
mark time. A stop book is not completed-horizon wealth; unobserved months are not
zero-valued observations. Policy intentions, attempted requests and actual paid
consumption are distinct. Tax and contractual liabilities are not inferred from
a generic spending shortfall.

The configured runner still has grouped funding and expanded housing/PE
behavior not supported by the common action session. Moving a caller requires
explicit treatment of those differences, not a compatibility wrapper or removal
of its financial coverage.

## References

- <DESIGN.md>: current preparation, execution and output boundaries.
- <REQUIREMENTS.md>: capability requirements and limitations.
- <docs/tax_engine_evaluation.md>: tax engine build-versus-adopt evaluation.
