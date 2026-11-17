package grpcmethods

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
)

type callerKey struct{}

// Caller selects a fixture connection presenting this exact client certificate. The
// selector stays local to the test; authorization metadata is passed unchanged to the server.
func Caller(ctx context.Context, uri string) context.Context {
	return context.WithValue(ctx, callerKey{}, uri)
}

type tlsConnections struct{ connections map[string]*grpc.ClientConn }

func (c *tlsConnections) connection(ctx context.Context) *grpc.ClientConn {
	uri, _ := ctx.Value(callerKey{}).(string)
	if connection := c.connections[uri]; connection != nil {
		return connection
	}
	panic("unknown fixture caller: " + uri)
}

func (c *tlsConnections) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	return c.connection(ctx).Invoke(ctx, method, args, reply, opts...)
}

func (c *tlsConnections) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	return c.connection(ctx).NewStream(ctx, desc, method, opts...)
}

// ServeTLS exercises method policies over real TLS with separately issued client identities.
// Requests without a Caller selector use server-authenticated TLS without a client leaf.
func ServeTLS(t *testing.T, serverURI string, methods auth.Methods, register func(*grpc.Server), callers ...string) grpc.ClientConnInterface {
	t.Helper()
	return ServeTLSWithTimeout(t, 5*time.Second, serverURI, methods, register, callers...)
}

// ServeTLSWithTimeout serves with the given method-admission timeout, including zero.
func ServeTLSWithTimeout(t *testing.T, admissionTimeout time.Duration, serverURI string, methods auth.Methods, register func(*grpc.Server), callers ...string) grpc.ClientConnInterface {
	t.Helper()
	ca := peertlstesting.NewCA(t)
	serving := ca.Issue(t, serverURI)
	serverTLS, err := serving.WorkloadServerTLSConfigWithServices(serverURI, true)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(append(methods.ServerOptions(admissionTimeout), grpc.Creds(credentials.NewTLS(serverTLS)))...)
	register(server)
	address := grpcserver.Serve(t, server)
	connections := &tlsConnections{connections: map[string]*grpc.ClientConn{}}
	for _, uri := range append([]string{""}, callers...) {
		if connections.connections[uri] != nil {
			continue
		}
		config, err := (peertls.ClientTLS{CAFile: ca.RootFile, ServerURI: serverURI}).TLSConfig()
		if err != nil {
			t.Fatal(err)
		}
		if uri != "" {
			leaf := ca.Issue(t, uri)
			config, err = leaf.ClientTLSConfig(serverURI)
			if err != nil {
				t.Fatal(err)
			}
		}
		connection, err := grpc.NewClient(address, grpc.WithNoProxy(), grpc.WithTransportCredentials(credentials.NewTLS(config)))
		if err != nil {
			t.Fatal(err)
		}
		connections.connections[uri] = connection
		t.Cleanup(func() { _ = connection.Close() })
	}
	return connections
}

// AsCaller binds a fixture client to one of ServeTLS's client connections.
func AsCaller(connection grpc.ClientConnInterface, uri string) grpc.ClientConnInterface {
	return tlsConnection{connection: connection, uri: uri}
}

type tlsConnection struct {
	connection grpc.ClientConnInterface
	uri        string
}

func (c tlsConnection) Invoke(ctx context.Context, method string, args, reply any, opts ...grpc.CallOption) error {
	return c.connection.Invoke(Caller(ctx, c.uri), method, args, reply, opts...)
}

func (c tlsConnection) NewStream(ctx context.Context, desc *grpc.StreamDesc, method string, opts ...grpc.CallOption) (grpc.ClientStream, error) {
	return c.connection.NewStream(Caller(ctx, c.uri), desc, method, opts...)
}
