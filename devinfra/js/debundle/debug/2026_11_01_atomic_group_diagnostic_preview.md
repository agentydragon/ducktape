# Atomic group diagnostic preview size

Observed in a private CI failure log on 2026-11-01: the 4,478-line job log
included an atomic-group header 13,634 characters long, 2,562 claim rows, and
1,146 sequenced initializer reason rows across repeated attempts. The run had
already written the complete `atomic_group_conflicts.json` report and linked it
from the materializer error, so repeating every member and claim in stderr made
the failure harder to scan without adding evidence.

The human summary now shows at most eight conflict groups and twenty members,
destinations, claims, and sequenced reason examples per group. It reports exact
total and omitted counts; when reason examples are shortened, it also retains
the complete per-rule counts. The serialized conflict report and the separate
cycle summary remain unchanged.
