# Firecracker vsock dialing

`Dial` opens Firecracker's host Unix socket, sends `CONNECT <port>\n`, and
returns the guest stream after a bounded `OK <port>\n` handshake. It validates
the response port and preserves stream bytes delivered in the same read as the
handshake. The context bounds connection setup and the handshake; on success,
the returned `net.Conn` has no deadline or context cancellation hook.

A gRPC client can use `Dial` as its context dialer:

```go
connection, err := grpc.NewClient(
    "passthrough:///guest-management",
    grpc.WithTransportCredentials(insecure.NewCredentials()),
    grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
        return vsock.Dial(ctx, socketPath, port)
    }),
)
```

The package and its tests use only the Go standard library; it has no gRPC
dependency.
