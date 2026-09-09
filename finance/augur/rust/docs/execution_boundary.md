# Execution input

`sim.backend.compile_run` prepares a self-contained execution document from a
`Scenario`, materialized paths, jurisdiction rules and locations. The engine
consumes that document directly; it never rereads the authoring inputs.

`sim/compiler/execution.py` owns preparation: resolve tax rules for each filing
profile, validate sampled values, and convert money, quantities and rates into
the engine's exact integer units. It does not allocate a second world model with
cashflow slots, lot masks, or precomputed obligation tensors. Runtime scheduling,
holdings, FIFO sales and settlement belong to Rust.

The production Rust input type is `ExecutionInput` in `rust/execution.rs`.
JSON is currently the Python/Rust transport. The Python writer and Rust fields
spell the transport schema separately. Unsupported series types are refused,
and Rust validates the resulting input before execution.

## Native experiment invocation

`rust/invocation.py` writes a `CompiledRun` once with `write_prepared_input` and
invokes an experiment-owned binary with `invoke`. The common argument prefix is
`INPUT.json OUTPUT.json`; remaining arguments belong to the experiment. The return
value is the existing decoded output document for experiment-owned analysis.
Native errors propagate before output is decoded; paths and arguments are passed
as argv entries, not shell text.

The separate `augur_native_invocation` Rust library loads that input through
`run` and checks serialization and buffered-write errors through `write_output`.
The supplied closure still chooses its engine entrypoint, policies, account
bindings, parameters and selected traces. Financial validation stays in the
engine. Neither helper changes path identities or caches execution state.

The bounded-spending and allocation-glide examples share this file transport.
Their ordinary policy functions remain compiled into their own small binaries;
this does not expose Python callbacks or select policies from a registry.

## Precision and validation

- Authored money must be exactly representable in its currency quantum.
- Sampled prices are rounded once to currency quanta; distribution rates retain
  sub-quantum precision until multiplied by holdings.
- CPI and other index levels cross as integer parts per billion. Raw-value
  checks precede quantization where rounding could hide invalid inputs.
- Tax preparation retains filing-status support checks and rejects jurisdictions
  that disagree on the taxpayer's shared capital-loss offset cap.
- Derived amounts, such as a property's building share, use the engine's checked
  integer arithmetic and rounding; intermediate products need not be exact
  currency amounts before rounding.

Tests author ordinary scenarios and run the same preparation. Test helpers may
copy or serialize the prepared document, but do not maintain another encoder or
tax-rule interpretation. Exact-money, statute, funding, lifecycle and rollout
acceptance tests exercise this same production path.
