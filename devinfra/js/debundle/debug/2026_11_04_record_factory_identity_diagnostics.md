# Local record-factory identity diagnostics

At base `02d9d165`, the CLI report used one `factory_identity` reason for
several different local constructor-factory refusals. A synthetic forwarding
control with an earlier `make` call preserved Node output (`169 0\n`) while
the report gave no clue whether export, publication, whole-chunk flow, or the
current prefix caused the later call to stay conservative. This made the
diagnostic difficult to use when tracing a source-level factory identity
boundary.

The report now identifies the first failing identity guard in its existing
evaluation order. For `factory_escaped`, it may include
`first_callback_publication_owner`: the first top-level source owner observed
inserting the function allocation into the escaped-callback set through
`publish_values`. This field records only an insertion observed at that path.
An absent value leaves the origin unknown; other insertion paths and
insertions without a current top-level owner are not reconstructed later.
The owner is diagnostic context and does not participate in any proof or
acceptance decision. See [Synchronous execution diagnostics](../SPEC.md#synchronous-execution-diagnostics)
for the report contract.

Use the reason and optional owner to identify which guard blocked the call and
where the tracked publication first entered the execution prefix. Continue
source tracing from that owner when investigating the runtime behavior; this
diagnostic alone does not establish which source operation caused publication.
