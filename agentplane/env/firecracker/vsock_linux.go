package firecracker

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"sync"
	"sync/atomic"
	"time"

	managementpb "github.com/agentydragon/ducktape/agentplane/env/firecracker/managementpb"
	"golang.org/x/sys/unix"
	"google.golang.org/grpc"
)

type vsockAddr struct {
	cid  uint32
	port uint32
}

func setRealtimeClock(value time.Time) error {
	return unix.ClockSettime(unix.CLOCK_REALTIME, &unix.Timespec{Sec: value.Unix(), Nsec: int64(value.Nanosecond())})
}

func (address vsockAddr) Network() string { return "vsock" }
func (address vsockAddr) String() string  { return fmt.Sprintf("%d:%d", address.cid, address.port) }

type vsockConn struct {
	file       *os.File
	localAddr  vsockAddr
	remoteAddr vsockAddr
}

func (connection *vsockConn) Read(destination []byte) (int, error) {
	return connection.file.Read(destination)
}

func (connection *vsockConn) Write(source []byte) (int, error) { return connection.file.Write(source) }
func (connection *vsockConn) Close() error                     { return connection.file.Close() }
func (connection *vsockConn) LocalAddr() net.Addr              { return connection.localAddr }
func (connection *vsockConn) RemoteAddr() net.Addr             { return connection.remoteAddr }
func (connection *vsockConn) SetDeadline(deadline time.Time) error {
	return connection.file.SetDeadline(deadline)
}

func (connection *vsockConn) SetReadDeadline(deadline time.Time) error {
	return connection.file.SetReadDeadline(deadline)
}

func (connection *vsockConn) SetWriteDeadline(deadline time.Time) error {
	return connection.file.SetWriteDeadline(deadline)
}

type vsockListener struct {
	fd         int
	closed     atomic.Bool
	localAddr  vsockAddr
	closeOnce  sync.Once
	closeError error
}

func listenVsock(port uint32) (*vsockListener, error) {
	fd, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC|unix.SOCK_NONBLOCK, 0)
	if err != nil {
		return nil, fmt.Errorf("create vsock listener: %w", err)
	}
	if err := unix.Bind(fd, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("bind vsock listener on port %d: %w", port, err)
	}
	if err := unix.Listen(fd, 8); err != nil {
		_ = unix.Close(fd)
		return nil, fmt.Errorf("listen on vsock port %d: %w", port, err)
	}
	return &vsockListener{
		fd:        fd,
		localAddr: vsockAddr{cid: unix.VMADDR_CID_ANY, port: port},
	}, nil
}

func (listener *vsockListener) Accept() (net.Conn, error) {
	for {
		if listener.closed.Load() {
			return nil, net.ErrClosed
		}
		connectionFD, address, err := unix.Accept4(listener.fd, unix.SOCK_CLOEXEC|unix.SOCK_NONBLOCK)
		if err != nil {
			if errors.Is(err, unix.EINTR) {
				continue
			}
			if errors.Is(err, unix.EAGAIN) || errors.Is(err, unix.EWOULDBLOCK) {
				pollFD := []unix.PollFd{{Fd: int32(listener.fd), Events: unix.POLLIN}}
				if _, err := unix.Poll(pollFD, 250); err != nil && !errors.Is(err, unix.EINTR) {
					if listener.closed.Load() {
						return nil, net.ErrClosed
					}
					return nil, fmt.Errorf("wait for guest management vsock connection: %w", err)
				}
				continue
			}
			if listener.closed.Load() {
				return nil, net.ErrClosed
			}
			return nil, fmt.Errorf("accept guest management vsock connection: %w", err)
		}
		peer, ok := address.(*unix.SockaddrVM)
		if !ok || !isAllowedPeerCID(peer.CID) {
			_ = unix.Close(connectionFD)
			continue
		}
		return &vsockConn{
			file:      os.NewFile(uintptr(connectionFD), "agentplane-firecracker-vsock-client"),
			localAddr: listener.localAddr,
			remoteAddr: vsockAddr{
				cid:  peer.CID,
				port: peer.Port,
			},
		}, nil
	}
}

func (listener *vsockListener) Close() error {
	listener.closeOnce.Do(func() {
		listener.closed.Store(true)
		_ = unix.Shutdown(listener.fd, unix.SHUT_RDWR)
		listener.closeError = unix.Close(listener.fd)
	})
	return listener.closeError
}

func (listener *vsockListener) Addr() net.Addr { return listener.localAddr }

func isAllowedPeerCID(peerCID uint32) bool {
	return peerCID == unix.VMADDR_CID_HOST
}

func serveVsock(ctx context.Context, port uint32, service managementpb.GuestManagementServer) error {
	listener, err := listenVsock(port)
	if err != nil {
		return err
	}
	defer listener.Close()
	server := grpc.NewServer(grpc.MaxRecvMsgSize(maxManagementMessageBytes))
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

var (
	_ net.Listener = (*vsockListener)(nil)
	_ net.Conn     = (*vsockConn)(nil)
)
