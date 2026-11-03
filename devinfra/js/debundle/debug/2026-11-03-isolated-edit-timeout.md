# Isolated edit example timeout

PR #14118's [first CI run][first] timed out in
`//devinfra/js/debundle/skills/shared/examples:isolated_edit_test`.
The only worker output was `baseline.invocation.json`: the first
`snapshot verify` had started but had no result or captured streams before
the one-minute test deadline. Native target metadata reports 66.564 seconds
including timeout handling. This does not identify a hashing, I/O or execution
cause.

The isolated RBE [rerun][isolated] passed the full example in 42.46 pytest
seconds, 57.111 target seconds. The [hosted job rerun][hosted] also passed;
its pytest log reports 37.50 seconds. No source or timeout change was made.
The queried prior target history contains passes mostly between 24 and 54
seconds. The first failure's cause remains open.

If this recurs, retain native action resource metadata and incremental subprocess
streams around snapshot verification before changing the deadline. The existing
invocation receipt establishes the attempted command, not its execution progress.

**Provenance:** CI records synthetic merge `f8eb53d7efe36db271c48d4cb6eebf7a364d2875`,
whose parents include PR head `6ead009a13aba8828459fa67958315921892daa8`.
Both commits have tree `08a0fa3aefab63a0510660b6cad40c8197f2266f`.
A different commit ID alone does not establish different tested source;
compare parents and trees.

[first]: https://app.buildbuddy.io/invocation/c66ebc6e-a4de-5130-a1f1-7668a8aae9a5
[isolated]: https://app.buildbuddy.io/invocation/14d82ef4-1a7a-4850-9916-38dc0290c3a7
[hosted]: https://app.buildbuddy.io/invocation/bb721c9b-fc00-5424-aeef-c202a6213fb5
