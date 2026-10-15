# grpc-gateway integration spike

This is an isolated proof of the generated HTTP/JSON facade proposed in
[`runner_delivery_and_harness_design.md`](../../docs/runner_delivery_and_harness_design.md).
It wires an annotated proto through grpc-gateway's upstream Bazel compiler and runs the generated
handler against a real in-process gRPC server. The root `MODULE.bazel` pins the upstream v2.28.0
Bazel module by commit; its module supplies the `go_proto_compiler` used here. Generated Go is a
Bazel build output; this directory does not check in generated code or add a production endpoint.

Run the proof with:

```bash
bbr test //agentplane/x/grpc_gateway:grpc_gateway_test
```

The test covers a unary request/response and a server-streaming watch, including lower-camel
ProtoJSON fields, quoted 64-bit values, query mapping, `Authorization` forwarding and rejection,
HTTP mapping for a unary error, `Grpc-Timeout` deadline propagation, the stream's terminal error
chunk, and HTTP cancellation reaching the gRPC stream.

The timeout case checks that the service receives a deadline shorter than the HTTP client's
fallback timeout, the request maps to HTTP 504, and the server handler finishes. Its context may
report either `Canceled` or `DeadlineExceeded` as the gateway and server timeout contexts expire.

The streaming endpoint is an ordinary server stream. grpc-gateway writes newline-delimited JSON
chunks. Once the first event has been sent, a later gRPC error is represented by a terminal JSON
error chunk; the HTTP response has already started and stays status 200. Reconnect and resume are
application-owned protocols: this generated facade does not provide durable subscriptions, replay,
or event cursors. Full-duplex client streaming remains on native gRPC.

The token check is only a fixture proving that the HTTP `Authorization` value reaches gRPC metadata
and that a service can reject an unauthenticated request. It does not define Agentplane's auth or
authorization boundary. The spike changes no production service, API, deployment, or security
policy.
