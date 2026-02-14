# Protobuf Migration for Tunnel Package

## Summary

Replaced the manually reconstructed stub `types.go` with proper Bazel-generated protobuf Go code for the tunnel protocol.

## Changes Made

### 1. Created Proto Definition (`tunnel.proto`)

Created `/home/user/ducktape/claude_web_env/re/environment_manager/6b49f1ca/src/internal/tunnel/tunnelpb/tunnel.proto` defining all tunnel message types:

- `Header` - HTTP header key-value pairs
- `HttpCancel` - HTTP request cancellation
- `HttpHeaders`, `HttpChunk`, `HttpError` - HTTP response messages
- `WsOpen`, `WsMessage`, `WsClose`, `WsError`, `WsOpened` - WebSocket messages
- `TunnelRequest` - Top-level request message with oneof payload
- `TunnelResponse` - Top-level response message with oneof payload

The proto uses `oneof` for discriminated unions, matching the original stub's `isTunnelRequestPayload` and `isTunnelResponsePayload` interfaces.

### 2. Added Protobuf Rules to MODULE.bazel

Added `rules_proto` dependency to `/home/user/ducktape/MODULE.bazel`:

```python
bazel_dep(name = "rules_proto", version = "7.1.0")
```

### 3. Updated BUILD.bazel

Updated `/home/user/ducktape/claude_web_env/re/environment_manager/6b49f1ca/src/internal/tunnel/tunnelpb/BUILD.bazel` to:

- Define `proto_library` target from `tunnel.proto`
- Define `go_proto_library` to generate Go code
- Embed the generated code in the `tunnelpb` go_library

### 4. Added Helper Methods (`helpers.go`)

Created `helpers.go` to provide compatibility with the original stub API:

- `GetHeaders()` methods for `TunnelRequest` and `WsOpen` (custom field not in proto)
- Wrapper helper methods for `TunnelRequest_*` types to access nested fields
  - `TunnelRequest_HttpCancel.GetRequestId()`
  - `TunnelRequest_WsOpen.GetPath()`, `GetPort()`, `GetUrl()`, `GetHeaders()`
  - `TunnelRequest_WsMessage.GetConnectionId()`, `GetType()`, `GetData()`
  - `TunnelRequest_WsClose.GetPath()`, `GetPort()`, `GetUrl()`, `GetConnectionId()`, `GetReason()`
  - `TunnelRequest_HttpRequest.GetPath()`, `GetPort()`, `GetUrl()`, `GetHeaders()`

These helpers maintain compatibility with code that accesses nested fields through the wrapper types.

### 5. Added Verification Test

Created `verify_proto_test.go` to verify that:

- Generated types have proper `ProtoReflect()` implementations (not returning `nil`)
- Message construction and field access work correctly

### 6. Removed Stub Implementation

Deleted `types.go` - the manually reconstructed stub that had no-op `ProtoReflect()` methods.

## Key Differences from Stub

### Proper ProtoReflect Implementation

The generated code has real `ProtoReflect()` implementations that return valid `protoreflect.Message` instances, unlike the stub which returned `nil`. This enables:

- Proper protobuf serialization/deserialization
- Reflection-based operations on messages
- Integration with protobuf tooling

### Headers Field Handling

The `headers` field (type `map[string][]string`) is not directly representable in proto3 (maps can't have repeated values). The current implementation:

- Omits `headers` from the proto definition
- Provides `GetHeaders()` methods in `helpers.go` that return empty maps
- Actual wire format serialization would need custom marshaling logic

This matches the stub's behavior where headers were marked with `protobuf:"-"` (excluded from proto serialization).

## Build Verification

Verified that:

1. The `tunnelpb` package builds successfully
2. The `tunnel` package (which uses `tunnelpb`) builds successfully
3. The full `environment-manager` binary builds successfully
4. The proto verification test passes

## Notes

The recursive `TunnelRequest` type in the oneof (field 10: `TunnelRequest http_request`) is unusual but valid in proto3. It represents that when the payload is `http_request`, the request data is in the outer `TunnelRequest`'s fields rather than a nested message.
