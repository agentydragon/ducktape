// Package httpconnect opens HTTP CONNECT tunnels over existing connections.
package httpconnect

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"
)

const handshakeTimeout = 10 * time.Second

// Open sends a CONNECT request over connection and returns the established
// tunnel. The caller supplies the already-dialed connection and the only
// request headers to send. Calling Open transfers ownership of connection:
// Open closes it on every error, and the caller owns the returned connection
// on success.
func Open(ctx context.Context, connection net.Conn, authority string, headers http.Header) (net.Conn, error) {
	keep := false
	defer func() {
		if !keep {
			_ = connection.Close()
		}
	}()

	handshakeCtx, cancel := context.WithTimeout(ctx, handshakeTimeout)
	defer cancel()
	deadline, _ := handshakeCtx.Deadline()
	if err := connection.SetDeadline(deadline); err != nil {
		return nil, fmt.Errorf("set CONNECT handshake deadline: %w", err)
	}

	interrupted := make(chan struct{})
	stopInterrupt := context.AfterFunc(handshakeCtx, func() {
		_ = connection.Close()
		close(interrupted)
	})
	defer func() {
		if !keep {
			if !stopInterrupt() {
				<-interrupted
			}
		}
	}()

	request := &http.Request{
		Method: http.MethodConnect,
		URL:    &url.URL{Host: authority},
		Host:   authority,
		Header: headers.Clone(),
	}
	if err := request.WriteProxy(connection); err != nil {
		return nil, handshakeError(handshakeCtx, authority, "write request", err)
	}
	reader := bufio.NewReader(connection)
	response, err := http.ReadResponse(reader, request)
	if err != nil {
		return nil, handshakeError(handshakeCtx, authority, "read response", err)
	}
	if response.StatusCode != http.StatusOK {
		_ = connection.Close()
		if response.Body != nil {
			_ = response.Body.Close()
		}
		return nil, fmt.Errorf("CONNECT to %q refused: %s", authority, response.Status)
	}
	if err := handshakeCtx.Err(); err != nil {
		return nil, fmt.Errorf("CONNECT handshake canceled: %w", err)
	}
	if err := connection.SetDeadline(time.Time{}); err != nil {
		return nil, fmt.Errorf("clear CONNECT handshake deadline: %w", err)
	}
	if !stopInterrupt() {
		<-interrupted
		return nil, fmt.Errorf("CONNECT handshake canceled: %w", handshakeCtx.Err())
	}
	keep = true
	return &bufferedConn{Conn: connection, reader: reader}, nil
}

func handshakeError(ctx context.Context, authority, operation string, err error) error {
	if contextErr := ctx.Err(); contextErr != nil {
		return fmt.Errorf("CONNECT handshake canceled: %w", contextErr)
	}
	return fmt.Errorf("%s CONNECT to %q: %w", operation, authority, err)
}

type bufferedConn struct {
	net.Conn
	reader *bufio.Reader
}

func (connection *bufferedConn) Read(p []byte) (int, error) {
	return connection.reader.Read(p)
}
