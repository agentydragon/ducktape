package vsock

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"strconv"
	"strings"
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
