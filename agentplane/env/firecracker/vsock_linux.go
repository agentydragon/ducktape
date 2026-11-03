package firecracker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"os"
	"time"

	"golang.org/x/sys/unix"
)

func setRealtimeClock(value time.Time) error {
	return unix.ClockSettime(unix.CLOCK_REALTIME, &unix.Timespec{Sec: value.Unix(), Nsec: int64(value.Nanosecond())})
}

func serveVsock(
	ctx context.Context,
	port uint32,
	bootID string,
	files credentialFiles,
	setClock clockSetter,
	onFirstThaw func() error,
) error {
	listener, err := unix.Socket(unix.AF_VSOCK, unix.SOCK_STREAM|unix.SOCK_CLOEXEC, 0)
	if err != nil {
		return fmt.Errorf("create vsock listener: %w", err)
	}
	defer unix.Close(listener)
	if err := unix.Bind(listener, &unix.SockaddrVM{CID: unix.VMADDR_CID_ANY, Port: port}); err != nil {
		return fmt.Errorf("bind vsock listener on port %d: %w", port, err)
	}
	if err := unix.Listen(listener, 8); err != nil {
		return fmt.Errorf("listen on vsock port %d: %w", port, err)
	}
	go func() {
		<-ctx.Done()
		_ = unix.Shutdown(listener, unix.SHUT_RDWR)
	}()

	ready := false
	for {
		connectionFD, peer, err := unix.Accept4(listener, unix.SOCK_CLOEXEC)
		if err != nil {
			if errors.Is(err, unix.EINTR) {
				continue
			}
			if ctx.Err() != nil {
				return nil
			}
			return fmt.Errorf("accept vsock thaw connection: %w", err)
		}
		peerVM, ok := peer.(*unix.SockaddrVM)
		if !ok {
			_ = unix.Close(connectionFD)
			continue
		}
		if err := unix.SetsockoptTimeval(connectionFD, unix.SOL_SOCKET, unix.SO_RCVTIMEO, &unix.Timeval{Sec: 30}); err != nil {
			_ = unix.Close(connectionFD)
			return fmt.Errorf("set vsock receive timeout: %w", err)
		}
		if err := unix.SetsockoptTimeval(connectionFD, unix.SOL_SOCKET, unix.SO_SNDTIMEO, &unix.Timeval{Sec: 30}); err != nil {
			_ = unix.Close(connectionFD)
			return fmt.Errorf("set vsock send timeout: %w", err)
		}
		connection := os.NewFile(uintptr(connectionFD), "agentplane-firecracker-thaw")
		if err := handleThaw(connection, peerVM.CID, unix.VMADDR_CID_HOST, bootID, files, setClock, onFirstThaw, &ready); err != nil {
			fmt.Fprintf(os.Stderr, "reject Firecracker thaw request: %v\n", err)
		}
		if err := connection.Close(); err != nil && !errors.Is(err, io.ErrClosedPipe) {
			fmt.Fprintf(os.Stderr, "close Firecracker thaw connection: %v\n", err)
		}
	}
}
