# Remaining staging decisions

The current analysis identity and reuse contract is in
<../docs/bazel_integration.md>; native-premise source identity is specified in
<../SPEC.md#native-integrity-inputs> and <../docs/wire_format.md>.

Per-chunk S3 actions and a separate vendor-strip action remain deferred until a
measured edit mix shows that their additional action and artifact costs pay
back. No measurement in this plan establishes that need.
