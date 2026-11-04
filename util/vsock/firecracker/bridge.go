package firecracker

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
	"sync"
	"time"
)

const maxHandshakeBytes = 128

// Dial opens the Firecracker vsock proxy socket and requests the guest port.
// The context controls both connection setup and the CONNECT handshake.
func Dial(ctx context.Context, socketPath string, port uint32) (net.Conn, error) {
	connection, err := (&net.Dialer{}).DialContext(ctx, "unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("connect to Firecracker vsock proxy %q: %w", socketPath, err)
	}
	stopClose := context.AfterFunc(ctx, func() { _ = connection.Close() })
	keepOpen := false
	defer func() {
		stopClose()
		if !keepOpen {
			_ = connection.Close()
		}
	}()

	if deadline, ok := ctx.Deadline(); ok {
		if err := connection.SetDeadline(deadline); err != nil {
			return nil, fmt.Errorf("set Firecracker vsock handshake deadline: %w", err)
		}
	}
	if _, err := fmt.Fprintf(connection, "CONNECT %d\n", port); err != nil {
		return nil, fmt.Errorf("request Firecracker vsock port %d: %w", port, err)
	}

	var response strings.Builder
	for range maxHandshakeBytes {
		var next [1]byte
		if _, err := io.ReadFull(connection, next[:]); err != nil {
			return nil, fmt.Errorf("read Firecracker vsock handshake: %w", err)
		}
		response.WriteByte(next[0])
		if next[0] == '\n' {
			line := response.String()
			if err := validateHandshakeResponse(line); err != nil {
				return nil, fmt.Errorf("Firecracker vsock rejected port %d: %q", port, strings.TrimSpace(line))
			}
			if err := connection.SetDeadline(time.Time{}); err != nil {
				return nil, fmt.Errorf("clear Firecracker vsock handshake deadline: %w", err)
			}
			if err := ctx.Err(); err != nil {
				return nil, fmt.Errorf("Firecracker vsock handshake canceled: %w", err)
			}
			if !stopClose() {
				return nil, fmt.Errorf("Firecracker vsock handshake canceled: %w", ctx.Err())
			}
			keepOpen = true
			return connection, nil
		}
	}
	return nil, fmt.Errorf("Firecracker vsock handshake exceeded %d bytes", maxHandshakeBytes)
}

func validateHandshakeResponse(line string) error {
	if !strings.HasSuffix(line, "\n") {
		return errors.New("response is not a complete line")
	}
	line = strings.TrimSuffix(line, "\n")
	if !strings.HasPrefix(line, "OK ") {
		return errors.New("response does not start with OK")
	}
	portText := strings.TrimPrefix(line, "OK ")
	if portText == "" {
		return errors.New("response has no port")
	}
	for _, digit := range []byte(portText) {
		if digit < '0' || digit > '9' {
			return errors.New("response port is not decimal")
		}
	}
	if _, err := strconv.ParseUint(portText, 10, 32); err != nil {
		return errors.New("response port is outside the uint32 range")
	}
	return nil
}

// Serve accepts local stream connections and bridges each to the requested
// guest port. It takes ownership of listener and closes it when Serve returns;
// filesystem socket cleanup follows the listener's close behavior.
func Serve(ctx context.Context, listener net.Listener, socketPath string, port uint32, handshakeTimeout time.Duration) error {
	if listener == nil {
		return errors.New("local listener is required")
	}
	defer listener.Close()
	if handshakeTimeout <= 0 {
		return errors.New("handshake timeout must be positive")
	}
	serveCtx, cancelServe := context.WithCancel(ctx)

	var connectionsMu sync.Mutex
	connections := make(map[net.Conn]struct{})
	var handlers sync.WaitGroup
	closeConnections := func() {
		connectionsMu.Lock()
		defer connectionsMu.Unlock()
		for connection := range connections {
			_ = connection.Close()
		}
	}
	shutdownDone := make(chan struct{})
	stopShutdown := context.AfterFunc(serveCtx, func() {
		defer close(shutdownDone)
		_ = listener.Close()
		closeConnections()
	})
	defer func() {
		cancelServe()
		if !stopShutdown() {
			<-shutdownDone
		}
		_ = listener.Close()
		closeConnections()
		handlers.Wait()
	}()

	track := func(connection net.Conn) bool {
		connectionsMu.Lock()
		defer connectionsMu.Unlock()
		if serveCtx.Err() != nil {
			return false
		}
		connections[connection] = struct{}{}
		return true
	}
	untrack := func(connection net.Conn) {
		connectionsMu.Lock()
		defer connectionsMu.Unlock()
		delete(connections, connection)
	}

	for {
		localConnection, err := listener.Accept()
		if err != nil {
			if serveCtx.Err() != nil {
				return nil
			}
			return fmt.Errorf("accept local stream connection: %w", err)
		}
		if !track(localConnection) {
			_ = localConnection.Close()
			return nil
		}
		handlers.Add(1)
		go func() {
			defer handlers.Done()
			defer untrack(localConnection)
			defer localConnection.Close()

			handshakeCtx, cancelHandshake := context.WithTimeout(serveCtx, handshakeTimeout)
			guestConnection, err := Dial(handshakeCtx, socketPath, port)
			cancelHandshake()
			if err != nil || !track(guestConnection) {
				if guestConnection != nil {
					_ = guestConnection.Close()
				}
				return
			}
			defer untrack(guestConnection)
			defer guestConnection.Close()
			copyBidirectional(localConnection, guestConnection)
		}()
	}
}

func copyBidirectional(localConnection, guestConnection net.Conn) {
	copyResults := make(chan error, 2)
	go func() { copyResults <- copyHalf(guestConnection, localConnection) }()
	go func() { copyResults <- copyHalf(localConnection, guestConnection) }()
	if err := <-copyResults; err != nil {
		_ = localConnection.Close()
		_ = guestConnection.Close()
	}
	<-copyResults
}

func copyHalf(destination, source net.Conn) error {
	if _, err := io.Copy(destination, source); err != nil {
		return err
	}
	if closeWriter, ok := destination.(interface{ CloseWrite() error }); ok {
		return closeWriter.CloseWrite()
	}
	return destination.Close()
}
