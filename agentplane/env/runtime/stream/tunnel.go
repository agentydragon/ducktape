package stream

import (
	"context"
	"net"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/httpconnect"
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
			connection, err := (&net.Dialer{Timeout: 10 * time.Second}).DialContext(ctx, "tcp", proxyAddress)
			if err != nil {
				return nil, err
			}
			return httpconnect.Open(ctx, connection, address, nil)
		}),
	)
}
