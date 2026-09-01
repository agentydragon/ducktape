# Bazel gRPC demo

This small app demonstrates one `.proto` definition shared by a Python gRPC
backend and a browser TypeScript frontend. Bazel generates the Python and
gRPC-Web bindings; generated sources are not checked in.

Build the complete stack:

```bash
bbr build //x/grpc_demo:server_bin \
  //x/grpc_demo:grpcwebproxy \
  //x/grpc_demo:bundle \
  //x/grpc_demo:static_server_bin
```

Run these targets in separate terminals:

```bash
bb run //x/grpc_demo:server_bin
```

```bash
bb run //x/grpc_demo:grpcwebproxy -- \
  --backend_addr=127.0.0.1:50051 \
  --server_http_debug_port=8080 \
  --run_tls_server=false \
  --allowed_origins=http://127.0.0.1:8081,http://localhost:8081
```

```bash
bb run //x/grpc_demo:static_server_bin -- --port 8081
```

Open <http://127.0.0.1:8081>. The browser calls the gRPC-Web proxy on
`127.0.0.1:8080`; an alternate proxy endpoint can be supplied with the
`endpoint` query parameter. The Python service test exercises the same
generated service contract without requiring a running process.
