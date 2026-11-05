package firecracker

import (
	"context"
	"fmt"
	"net"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"github.com/mdlayher/vsock"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
)

// hostOnlyListener keeps the management service private to the VMM host.
// Socket lifecycle and deadlines belong to the native vsock implementation.
type hostOnlyListener struct {
	net.Listener
}

func (listener hostOnlyListener) Accept() (net.Conn, error) {
	for {
		connection, err := listener.Listener.Accept()
		if err != nil {
			return nil, err
		}
		peer, ok := connection.RemoteAddr().(*vsock.Addr)
		if ok && isAllowedPeerCID(peer.ContextID) {
			return connection, nil
		}
		_ = connection.Close()
	}
}

func isAllowedPeerCID(peerCID uint32) bool {
	return peerCID == vsock.Host
}

func setRealtimeClock(value time.Time) error {
	return unix.ClockSettime(unix.CLOCK_REALTIME, &unix.Timespec{Sec: value.Unix(), Nsec: int64(value.Nanosecond())})
}

func listenHostOnly(port uint32) (net.Listener, error) {
	listener, err := vsock.Listen(port, nil)
	if err != nil {
		return nil, err
	}
	return hostOnlyListener{Listener: listener}, nil
}

func serveVsock(ctx context.Context, port uint32, service managementpb.GuestManagementServer) error {
	listener, err := listenHostOnly(port)
	if err != nil {
		return err
	}
	defer listener.Close()
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxManagementMessageBytes), grpc.WaitForHandlers(true))
	managementpb.RegisterGuestManagementServer(server, service)
	serveErr := make(chan error, 1)
	go func() { serveErr <- server.Serve(listener) }()
	select {
	case <-ctx.Done():
		server.Stop()
		<-serveErr
		return nil
	case err := <-serveErr:
		if err != nil {
			return fmt.Errorf("serve guest management gRPC server: %w", err)
		}
		return nil
	}
}
