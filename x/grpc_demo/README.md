# Bazel gRPC demo

This small app demonstrates one `.proto` definition shared by a Python gRPC
backend and a TypeScript/Node frontend. Bazel generates both language bindings;
generated sources are not checked in.

Build both applications and run the backend in one terminal:

```bash
bbr build //x/grpc_demo:server_bin //x/grpc_demo:frontend_bin
bb run //x/grpc_demo:server_bin
```

Then, in another terminal, call the backend through the generated TypeScript
client:

```bash
bb run //x/grpc_demo:frontend_bin -- Ada
```

The frontend defaults to `127.0.0.1:50051`; set `GRPC_DEMO_ADDRESS` to use a
different host or port. The Python service test exercises the same generated
service contract without requiring a running process.
