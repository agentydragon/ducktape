# Firecracker vsock stream bridge

`Dial` opens Firecracker's host Unix socket, sends `CONNECT <port>\n`, and
returns the guest stream after a bounded `OK` line. The caller's context bounds
the connection and handshake; on success the socket deadline and context
cancellation hook are cleared. The returned `net.Conn` carries only guest
stream bytes, including bytes delivered in the same read as the handshake.

`Serve` accepts local stream connections and bridges each to the Firecracker
socket. It owns and closes the supplied `net.Listener`, bounds each guest
handshake with the supplied timeout, and closes pending and active connections
when the context is canceled. Closing a filesystem Unix listener also follows
that listener's socket-path cleanup behavior. Callers choose the port, timeout,
and local listener permissions. A gRPC client can use `Dial` as its
`grpc.WithContextDialer`; this package has no gRPC dependency or message
handling.
