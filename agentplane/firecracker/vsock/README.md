# Firecracker vsock transport

`Dial` opens Firecracker's host Unix socket, sends `CONNECT <port>\n`, and
returns the guest stream after a bounded `OK <port>\n` handshake. It validates
the response port and preserves stream bytes delivered in the same read as the
handshake. The context bounds connection setup and the handshake; on success,
the returned `net.Conn` has no deadline or context cancellation hook.

`DialListener` adapts an outbound dial function for a host gRPC server. It
retries failed dials with capped exponential backoff, and waits for each
accepted stream to close before dialing again. `AcceptDialer` adapts the guest's
native vsock listener for a gRPC client. It accepts host-created streams and
hands them to gRPC's context dialer; canceled dial requests are rejected so the
next host attempt can proceed. These adapters pass the byte stream through
unchanged and add no RPC framing.

With the host serving on guest port 53 and the guest connecting to that stream:

```go
hostListener := vsock.DialListener(ctx, func(ctx context.Context) (net.Conn, error) {
    return vsock.Dial(ctx, socketPath, 53)
})
go grpcServer.Serve(hostListener)

guestDialer := vsock.AcceptDialer(guestVsockListener)
defer guestDialer.Close()
guestClient, err := grpc.NewClient(
    "passthrough:///host-workload-api",
    grpc.WithTransportCredentials(insecure.NewCredentials()),
    grpc.WithContextDialer(guestDialer.DialContext),
)
```

The package implementation has no gRPC dependency. Its tests use gRPC's health
service to verify that an ordinary client RPC crosses both adapters.
