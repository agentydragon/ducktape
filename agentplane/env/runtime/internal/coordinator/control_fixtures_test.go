package coordinator

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/env/runtime/control"
	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/identity/credentials/workloadid"
	environmentruntimetransport "github.com/agentydragon/ducktape/agentplane/net/peertls"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
)

// serveControl serves r's control listener until the test ends and returns its address.
func serveControl(t *testing.T, r *Runtime) string {
	t.Helper()
	listener, server, err := r.control.Listen(r.executorProxy)
	if err != nil {
		t.Fatal(err)
	}
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return listener.Addr().String()
}

// clientCredentialSource is implemented by the transport security types a control client can dial with.
type clientCredentialSource interface {
	ClientCredentials(expectedPeerURI string) (credentials.TransportCredentials, error)
}

// dialControl connects to the control listener at address until the test ends.
func dialControl(t *testing.T, address string, source clientCredentialSource, expectedPeerURI string, options ...grpc.DialOption) *grpc.ClientConn {
	t.Helper()
	transport, err := source.ClientCredentials(expectedPeerURI)
	if err != nil {
		t.Fatal(err)
	}
	conn, err := grpc.NewClient(address, append([]grpc.DialOption{grpc.WithTransportCredentials(transport)}, options...)...)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return conn
}

// controlPeers dials a served control listener as the identities it admits. runtimeURI and ca are set only for mTLS:
// the Runtime's identity and the authority behind every certificate.
type controlPeers struct {
	address    string
	runtimeURI string
	ca         *environmentruntimetransporttesting.CA
}

// startControl starts a Runtime whose control listener uses mode. For mTLS it issues the Runtime's certificate from a
// new test CA, and the returned peers issue theirs from the same CA.
func startControl(t *testing.T, config runtimeTestConfig, mode environmentruntimetransport.SecurityMode) (*Runtime, controlPeers) {
	t.Helper()
	var peers controlPeers
	if mode == environmentruntimetransport.MutualTLS {
		var err error
		peers.ca = environmentruntimetransporttesting.NewCA(t)
		peers.runtimeURI, err = workloadid.ID{Namespace: "test", Name: "sandbox", CredentialID: "credential-id"}.URI()
		if err != nil {
			t.Fatal(err)
		}
		runtimeTLS := peers.ca.Issue(t, peers.runtimeURI)
		config.Control.Security = environmentruntimetransport.Security{Mode: mode, TLS: &runtimeTLS}
	}
	r := newLifecycleRuntime(t, config)
	peers.address = serveControl(t, r)
	return r, peers
}

// dial connects as identity, which an mTLS listener authorizes by certificate URI.
func (p controlPeers) dial(t *testing.T, identity string, options ...grpc.DialOption) *grpc.ClientConn {
	t.Helper()
	security := environmentruntimetransport.Security{Mode: environmentruntimetransport.Insecure}
	if p.ca != nil {
		clientTLS := p.ca.Issue(t, identity)
		security = environmentruntimetransport.Security{Mode: environmentruntimetransport.MutualTLS, TLS: &clientTLS}
	}
	return dialControl(t, p.address, security, p.runtimeURI, options...)
}

// bind has the Sandbox Controller bind the Runtime to incarnation, as production does before a Gateway can watch.
func (p controlPeers) bind(t *testing.T, sandboxUID, incarnation string) {
	t.Helper()
	controller := environmentruntimepb.NewEnvironmentRuntimeClient(p.dial(t, environmentruntimetransport.SandboxControllerURI))
	bindTestIncarnation(t, controller, sandboxUID, incarnation)
}

// fencedContext attaches the Sandbox, incarnation and boot identifiers that direct calls must present.
func fencedContext(ctx context.Context, sandboxUID, incarnation, bootID string) context.Context {
	return metadata.AppendToOutgoingContext(
		ctx,
		control.SandboxMetadata, sandboxUID,
		control.IncarnationMetadata, incarnation,
		control.BootMetadata, bootID,
	)
}
