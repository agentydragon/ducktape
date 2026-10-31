# K3s process trace collection and setup boundary, 2026-10-31

The test fixture now owns an OTLP/gRPC and OTLP/HTTP receiver. A test-only `monitoring/alloy`
Service and Endpoints route the production service/Runner exporters to it without a production
collector. Exports append immediately to `test.outputs/process-spans.jsonl` with trace, span,
parent, service, source and timing fields and an allowlist of useful identity, HTTP/RPC outcome and
numeric attributes. The test client injects the active W3C traceparent into app HTTP calls.
The receiver starts before k3s and closes after k3s teardown; SDK batching can still lose spans
from a process killed before it flushes. Request bodies and authorization fields are excluded.

## RBE proof

The first integrated VM test passed all six cases in
[`e7793336`](https://app.buildbuddy.io/invocation/e7793336-ed58-47eb-b193-cb56463198a1)
(outer `004de109-5cb6-48f1-aec4-38222a1a64eb`). Effective source was local
`3c16fe96e9` atop collector `3efbbc0f89` and OOM budget `3af9e37afb`; BuildBuddy metadata
`86f590ec` was the `bbr` sync base. The 7.2 MB `process-spans.jsonl` artifact has 12,648
spans from 12 service names, including 193 guest Runner spans. Its SHA-256 is
`f9207eedbe28a60145aa569ecc3dc5a6275571c07ebdae8ed40709b26757138f`.
The Python `vm.case` trace ID joins 3,964 process spans from app, history-service and
runner-gateway. Guest Runner spans have separate trace roots; join them by session/thread
identity and timestamps, and do not infer a guest parent from the driver trace. The
[native startup note](../../debug/vm_native_startup_tracing.md) gives same-Runner phase evidence.

That VM's `k3s-timeline.jsonl` reports node Ready at 8.7 s, fixture images imported at 51.5 s,
all images imported at 84.9 s, and stack ready at 102.0 s. The 27 individual driver-side image
imports total 68.2 s; the guest-root image alone took 33.2 s. Import overlaps other setup, so
68.2 s cannot be added to the 102.0 s stack duration. These values describe one worker and
cache state, not a fleet estimate or a process CPU profile. The VM timeline artifact's SHA-256
is `9fa6861f0b647610b9c05876d31a4ef9021cad660b9ffac3582528631b111885`.

The real dispatcher suite also passed in
[`fe718ab1`](https://app.buildbuddy.io/invocation/fe718ab1-001b-4292-ab19-63df30076bfc)
(outer `ce0c1c90-3b04-4744-8a33-c8b14dc71b28`) using `bbr` script mode with retries
disabled. It published `process-spans.jsonl` (2,938 spans, including 24 Runner spans) and
`containerd.log`. Its timeline reached stack ready at 55.6 s. These two tests used different
workers and suites; their stack durations are not a before/after comparison.

## Bundled containerd limit

The pinned K3s v1.34.12+k3s1 bundles containerd v2.2.7-k3s1. Upstream containerd supports
gRPC tracing via its OTLP tracing processor and requires an explicit `OTEL_SDK_DISABLED=false`
alongside an endpoint ([containerd 2.0 documentation](https://github.com/containerd/containerd/blob/main/docs/containerd-2.0.md),
[tracing documentation](https://github.com/containerd/containerd/blob/main/docs/tracing.md)).
The dispatcher probe supplied both through K3s's documented `CONTAINERD_` child-env prefix,
but received zero `service.name=containerd` spans. Its retained startup log, at 07:32:33 UTC,
shows containerd starting and many plugins loading; it contains no registration of
`io.containerd.tracing.processor.v1.otlp` or `io.containerd.internal.v1.tracing`. The log's
SHA-256 is `7a8a223404255fa54da1c3be014bc62a328ef3c0ff357eef01d9f72b0e72d2b4`.
The pinned containerd fork has a tracing builtins file guarded by `!no_tracing`, but these
artifacts do not establish whether K3s compiled tracing out or disabled it another way.
The fixture therefore does not claim containerd OTLP spans. It retains its daemon log on
each run, starting fresh even when a runner's image store is recycled. For VM stacks it
uses the bundled `k3s ctr pprof` client and a root-only containerd debug socket to capture
real CPU and Go runtime profiles while the guest image imports. The per-image timeline
still supplies wall-clock boundaries; the profiles identify sampled import stacks and
goroutine blocking, not a complete image-unpack wall-time breakdown.

The profiler-enabled VM suite passed in
[`dd710ebd`](https://app.buildbuddy.io/invocation/dd710ebd-75cc-4903-b9a8-191a1c5f6489)
(outer `3130cd22-f7cf-410b-8475-6b95bc8f2f52`). Its timeline reports node Ready
at 8.1 s, fixture imports at 49.9 s, guest-root import from about 50.3 to 82.4 s
(32.1 s), and stack ready at 98.1 s. Its `process-spans.jsonl` contains 12,732
spans from 12 service names, including 183 guest Runner spans (SHA-256
`fb96901362fa111cac7a735292623a61f9b3eda0ce47aa7472bb05525915b96a`).
The 15 s CPU profile contains 13.26 s of
samples, with 5.14 s cumulative (38.76% of samples) under containerd
`localTransferService.importStream` → archive `ImportIndex/onUntarBlob` →
`content.WriteBlob`. The 5 s runtime trace parses into a syscall-blocking profile;
1.69 s of its 2.18 s aggregated blocking duration belongs to that transfer/write path.
Those are CPU samples and goroutine-blocking sums, respectively, not fractions of stack
wall time. They directly show process-side image transfer and content writes; they do
not isolate overlay snapshot work or disk wait. The profiles' SHA-256 values are
`54e42c2b93ddf9560110c52320b628c0c201a3bdb9b026e1592f1714f45793cb`
(CPU) and `d113e7391685623fb05c0d2dc867dd900850bcd47cd282cfac5ca729c6da17ef`
(runtime trace). The matching timeline SHA-256 is
`c903bf7fc56b38f8d6098a6505761e8ad377c14e791362641c2758f519d701af`.
Profiling overlaps import and has no startup sleep, but its CPU and trace recording
have unmeasured perturbation. This run and the earlier VM test used different workers
and cache states, so their 98.1 s and 102.0 s stack times are not an overhead comparison.

To inspect the artifacts with the pinned Nix Go source, run
`GO111MODULE=off go run cmd/pprof -top -cum <downloaded.cpu.pprof>` and
`GO111MODULE=off go run cmd/trace -pprof=syscall <downloaded.runtime.trace> > syscall.pprof`
from `$(go env GOROOT)/src`, then inspect that derived pprof with the first command.

For reproduction, run `bbr test //agentplane/system_tests/suites:test_vm --nocache_test_results`
or a smaller k3s suite, then download `test.outputs/process-spans.jsonl`,
`test.outputs/k3s-timeline.jsonl`, `test.outputs/containerd.log` and (for VM suites)
the two `containerd-import.*` profiles from the child Bazel
invocation. The collector unit test, HTTP traceparent client test and rendered-fixture test passed
before the VM run; the final live VM and dispatcher targets above passed on RBE.
