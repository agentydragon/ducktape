# Python gRPC Stubs for BuildBuddy API

## Goal

Call BuildBuddy's `Run` RPC directly from Python to get clean
stdout/stderr from remote bazel queries, instead of shelling out to
`bb remote` which mixes its own log lines into stdout.

## Current State

- `bb remote query` works but contaminates stdout with ANSI-colored git
  sync messages, progress indicators, and BuildBuddy framing. We filter
  these out by only keeping lines starting with `//` or `@` (valid Bazel
  labels). This works but is fragile.
- The repo already has `py_proto_library` targets for BuildBuddy message
  types (`runner_proto`, `git_proto`, etc.) in
  `third_party/buildbuddy/BUILD.protos.bazel`.
- `grpcio==1.78.0` is in the pip lockfile as a transitive dep.
- Python gRPC service stubs (`*_pb2_grpc.py`) are **not** generated —
  only Go gets gRPC via `go_proto_library(..., compilers = go_grpc)`.

## What We Need

Python gRPC stubs for `BuildBuddyService` (specifically the `Run` RPC)
so we can call it via `grpcio` instead of subprocess + stdout filtering.

Relevant protos:

- `buildbuddy_service.proto` — service definition with `rpc Run(RunRequest) returns (RunResponse)`
- `runner.proto` — `RunRequest`, `RunResponse`, `Step`
- `git.proto` — `GitRepo`, `RepoState`
- `eventlog.proto` — `GetEventLogChunk` (for streaming logs)
- `context.proto` — `RequestContext`

## Approaches Tried

### A. `rules_proto_grpc_python` (BCR 5.0.1) — tried 2026-03

**Result: fails — Python version incompatibility.**

`rules_proto_grpc_python` 5.0.1 registers Python toolchains 3.8–3.12
and its own `pip.parse` hub. Our repo uses Python 3.13. The select on
Python version fails because none of the registered versions match.

### B. `rules_proto_grpc_python` from master via `git_override` — tried 2026-03

**Result: fails — `rules_python` version skew.**

Using `git_override` with `strip_prefix = "modules/python"` picks up
3.13 support, but the module's `rules_python` dep (0.34.0) conflicts
with our 1.9.0. The merged toolchain registrations try to resolve
Python `3.14.0b2` (registered by the `grpc` C++ dep, see below) against
the older `rules_python`'s `tool_versions` dict, which doesn't have it.

### A/B revisited. `rules_proto_grpc_python` (BCR 5.8.0) — investigated 2026-04

**Status: likely viable — earlier blockers are resolved.**

BCR `rules_proto_grpc_python` 5.8.0 depends on:

- `grpc` 1.74.1 (which depends on `protobuf` 31.1)
- `rules_python` 1.6.3

The original concerns about the `grpc` C++ Bazel module being "toxic"
(see Option C below) turn out to be **wrong for `rules_python` ≥1.0**:

1. **`is_default` from submodules is ignored.** `rules_python` 1.9.0
   only honors `is_default=True` from the root module. The `grpc`
   module's `is_default` on 3.14.0b2 (or 3.13) is silently overridden.
2. **Duplicate toolchain versions are deduplicated.** First-in-graph
   wins; others get a debug-level warning. No build failure.
3. **Separate `pip.parse` `hub_name` values don't conflict.** Each
   module gets its own pip repo namespace (`grpc_python_dependencies`,
   `rules_proto_grpc_python_pip_deps`, our `@pypi`).

**Remaining risk:** protobuf version skew. `grpc` 1.74.1 on BCR depends
on `protobuf` 31.1; we have 33.5. Bzlmod resolves to the max (33.5),
but gRPC 1.74.1 was tested against 31.1. Protobuf 31→33 may include
breaking C++ API changes that cause gRPC build failures.

The unreleased master of `rules_proto_grpc_python` uses `grpc` 1.78.0
and `protobuf` 34.0, which would be better aligned with our versions.

**Next step:** try `bazel_dep(name = "rules_proto_grpc_python", version = "5.8.0")`
and see if `bbr build //...` survives the protobuf version skew.

### C. Define plugins ourselves, use `@grpc//src/compiler:grpc_python_plugin`

**Result: believed to fail — `grpc` C++ module is toxic. Re-evaluated:
probably fine (see A/B revisited above).**

Adding `bazel_dep(name = "grpc", version = "1.78.0")` to get the
`grpc_python_plugin` binary pulls in a massive C++ module that:

1. Registers Python toolchains including `3.14.0b2`
2. Has its own `pip.parse` hub (`grpc_python_dependencies`)
3. Depends on `rules_python` 1.5.4, creating version skew
4. Pulls in 15+ transitive C++ deps (openssl, zlib, re2, etc.)

Originally believed to cause `key "3.14.0b2" not found in dictionary`
errors. **Re-evaluation (2026-04):** `rules_python` ≥1.0 handles
submodule toolchain registrations gracefully — `is_default` from
non-root modules is ignored, and duplicate versions are deduplicated.
The real risk is protobuf C++ version skew between gRPC's dep and ours,
not the Python toolchain registration.

### D. Genrule with `grpcio-tools` from pip

**Status: not yet attempted (blocked on adding dep).**

Use `grpcio-tools` (pip package) which bundles protoc + the gRPC Python
plugin. Run via genrule: `python -m grpc_tools.protoc --grpc_python_out=...`.

Requires:

1. Add `grpcio-tools>=1.78.0` to `pyproject.toml`
2. Regenerate lockfile (`bazel run //:requirements.update`)
3. Figure out the Bazel entry point name for the protoc binary inside
   the `grpcio_tools` wheel (likely
   `@pypi//grpcio_tools//:rules_python_wheel_entry_point_grpc_tools.protoc`)
4. Write genrule with correct `--proto_path` for transitive proto deps

**Pros:** No new Bazel module deps. Uses pip infrastructure we already
have. Avoids all the Python toolchain registration conflicts.

**Cons:** Genrule is more verbose than a dedicated rule. Need to manage
proto include paths manually. `grpcio-tools` adds ~15MB to the lockfile.

### E. Skip codegen, use `grpcio` directly with `channel.unary_unary()`

**Status: not attempted.**

Construct gRPC requests manually using the `py_proto_library` message
types we already have, and call RPCs by method name string via
`grpcio`'s dynamic API:

```python
channel = grpc.secure_channel("remote.buildbuddy.io:443", creds)
call = channel.unary_unary(
    "/runner.BuildBuddyService/Run",
    request_serializer=RunRequest.SerializeToString,
    response_deserializer=RunResponse.FromString,
)
response = call(request)
```

**Pros:** Zero codegen. No new deps. Works today with existing
`py_proto_library` + `grpcio`.

**Cons:** No IDE autocomplete on service stubs. Method names are magic
strings. Must manually implement streaming for `GetEventLogChunk`.

### F. Pin `protobuf` Bazel module to 29.5 via `single_version_override`

**Result: fails — abseil-cpp version incompatibility.**

`protobuf@29.5` needs an older `abseil-cpp` that has
`absl/utility:if_constexpr`, but the resolved `abseil-cpp` (pulled by
other modules) doesn't have that target. This is a C++ ABI
incompatibility — you can't mix protobuf 29.x with the abseil version
that protobuf 32.x's transitive deps bring in.

## Protobuf Version Skew — Resolved (2026-04)

The previously blocking three-way conflict between the Bazel `protobuf` module
gencode version, the pip `protobuf` runtime, and `autogen-core`'s `<6.0.0` pin
is now fully resolved:

- `autogen-core` was replaced by `agent-framework-core`, which has no protobuf pin.
- pip `protobuf` is pinned to `6.33.1` to match the nixpkgs version.
- `py_proto_library` targets now build and run correctly.

`py_proto_library` targets for BuildBuddy message types (including `invocation_py_proto`
and its transitive deps) are defined in `third_party/buildbuddy/BUILD.protos.bazel`.
These are used for JSON deserialization via `google.protobuf.json_format` — see
`devinfra/precommit/test_tag.py` for an example.

## Recommendation

Python gRPC **service** stubs (`*_pb2_grpc.py`) are still not generated — only Go
gets those. For the `Run` RPC use case (option E/D above), the path is now clear:
message types are available via `py_proto_library`; only service stub generation
remains as future work if needed. Option D (genrule with `grpcio-tools`) or option E
(manual `channel.unary_unary()`) are both viable starting points.
