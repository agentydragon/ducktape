# Saved purity protobuf prototype

The prototype migrates the purity evidence in real `.analysis` artifacts.
`saved_purity.proto` is the saved model; Bazel generates its Rust records.
`SavedPurityGraph`, `SavedPurity`, and `SavedReason` are removed. `PurityRule`
and `ProgramPremiseKey` use the generated enums directly.

Runtime `Purity` retains its owned verdicts and shared `Arc` causes. Its saved
form is a postorder table, with typed indices in the other saved facts. Causes
must refer to an earlier node. The protobuf decoder checks that constraint and
requires verdict, rule, and span presence before restoring runtime values.

Proof dependencies retain their runtime sets and distinct author-reference
types. Their one explicit encode/decode pair maps these into repeated protobuf
records. Source locations and SWC spans also cross an explicit boundary.
There is no additional handwritten saved model or conversion framework.

The rest of the artifact retains MessagePack/zstd, containing the native protobuf
table as bytes. This is a bounded format cutover, with regeneration enforced by
the existing analysis-engine identity. Migrating the rest of saved analysis is
separate work; the mixed container is not the final format.

The table now includes owned statement verdicts as well as shared causes. Loading
temporarily retains an `Arc` for each table entry before copying owned verdicts
into statement facts. This prototype makes no performance or memory-savings claim.
Correctness validation covers proof metadata, sharing, malformed references, and
direct-versus-saved CLI output parity.
