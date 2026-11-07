package stream

import (
	"bufio"
	"context"
	"fmt"
	"net"
	"net/http"
	"net/url"
	"time"

	"github.com/agentydragon/ducktape/agentplane/observability"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

// NewTunneledConnection carries end-to-end service TLS through the sidecar's
// HTTP CONNECT route. The sidecar authenticates its own hop; it cannot choose
// the service's caller. The supplied credentials verify the destination SVID.
func NewTunneledConnection(target, proxyAddress string, security credentials.TransportCredentials) (*grpc.ClientConn, error) {
	return grpc.NewClient(
		"passthrough:///"+target,
		observability.GRPCClientOption(), grpc.WithNoProxy(),
		grpc.WithTransportCredentials(security),
		grpc.WithContextDialer(func(ctx context.Context, address string) (net.Conn, error) {
			return connectTunnel(ctx, proxyAddress, address)
		}),
	)
}

func connectTunnel(ctx context.Context, proxyAddress, target string) (net.Conn, error) {
	connection, err := (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp", proxyAddress)
	if err != nil {
		return nil, err
	}
	connected := false
	defer func() {
		if !connected {
			_ = connection.Close()
		}
	}()
	deadline := time.Now().Add(10 * time.Second)
	if end, ok := ctx.Deadline(); ok && end.Before(deadline) {
		deadline = end
	}
	if err := connection.SetDeadline(deadline); err != nil {
		return nil, err
	}
	request := &http.Request{Method: http.MethodConnect, URL: &url.URL{Opaque: target}, Host: target, Header: make(http.Header)}
	if err := request.Write(connection); err != nil {
		return nil, err
	}
	reader := bufio.NewReader(connection)
	response, err := http.ReadResponse(reader, request)
	if err != nil {
		return nil, err
	}
	if response.StatusCode != http.StatusOK {
		_ = response.Body.Close()
		return nil, fmt.Errorf("platform CONNECT refused: HTTP %d", response.StatusCode)
	}
	if err := connection.SetDeadline(time.Time{}); err != nil {
		return nil, err
	}
	connected = true
	return &tunnelConnection{Conn: connection, reader: reader}, nil
}

type tunnelConnection struct {
	net.Conn
	reader *bufio.Reader
}

func (c *tunnelConnection) Read(bytes []byte) (int, error) { return c.reader.Read(bytes) }
