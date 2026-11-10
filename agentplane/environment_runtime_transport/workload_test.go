package environment_runtime_transport_test

import (
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

func TestOpenedSecurityUsesWorkloadAPIStreamForHandshakeAndPinsIdentity(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	runnerURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "workload-credential"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	initial := ca.Issue(t, runnerURI)
	rotated := ca.Issue(t, runnerURI)
	otherURI, err := environment_runtime_transport.WorkloadID{Namespace: "agent-ns", Name: "sandbox-uid", CredentialID: "other-credential"}.URI()
	if err != nil {
		t.Fatal(err)
	}
	changedIdentity := ca.Issue(t, otherURI)
	rootDER := readCertificateDER(t, ca.RootFile)

	workloadServer := newWorkloadAPITestServer(t, workloadX509SVID(t, initial, runnerURI, rootDER), rootDER)
	security := environment_runtime_transport.Security{
		Mode: environment_runtime_transport.MutualTLS,
		TLS:  &environment_runtime_transport.Config{WorkloadAPISocket: workloadServer.endpoint},
	}
	opened, err := security.Open(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	defer func() {
		if err := opened.Close(); err != nil {
			t.Errorf("close opened security: %v", err)
		}
	}()
	if got, err := opened.LocalIdentity(x509.ExtKeyUsageServerAuth); err != nil || got != runnerURI {
		t.Fatalf("LocalIdentity() = %q, %v; want %q", got, err, runnerURI)
	}

	gateway := ca.Issue(t, environment_runtime_transport.GatewayURI)
	clientCredentials, err := gateway.ClientCredentials(runnerURI)
	if err != nil {
		t.Fatal(err)
	}
	serverCredentials, err := opened.ServerCredentials(environment_runtime_transport.GatewayURI)
	if err != nil {
		t.Fatal(err)
	}
	initialInfo, _, err := handshake(t, clientCredentials, serverCredentials)
	if err != nil {
		t.Fatal(err)
	}
	if got := peerLeaf(t, initialInfo); string(got.Raw) != string(readCertificateDER(t, initial.CertFile)) {
		t.Fatal("initial Workload API handshake did not present the initial SVID")
	}

	rotationDone := make(chan struct{})
	go func() {
		defer close(rotationDone)
		for range 64 {
			select {
			case workloadServer.updates <- workloadX509SVID(t, rotated, runnerURI, rootDER):
			case <-t.Context().Done():
				return
			}
		}
	}()
	// Handshakes read bundles while the source replaces them. This target runs
	// with the race detector to cover the upstream X509Source rotation boundary.
	for range 32 {
		if _, _, err := handshake(t, clientCredentials, serverCredentials); err != nil {
			t.Fatalf("concurrent Workload API rotation: %v", err)
		}
	}
	<-rotationDone
	waitForWorkloadHandshake(t, clientCredentials, serverCredentials, readCertificateDER(t, rotated.CertFile))

	workloadServer.updates <- workloadX509SVID(t, changedIdentity, otherURI, rootDER)
	deadline := time.Now().Add(5 * time.Second)
	for {
		_, _, err = handshake(t, clientCredentials, serverCredentials)
		if err != nil && strings.Contains(err.Error(), "identity changed") {
			break
		}
		if time.Now().After(deadline) {
			t.Fatalf("identity switch was not rejected, last handshake error: %v", err)
		}
		time.Sleep(10 * time.Millisecond)
	}

	if err := opened.Close(); err != nil {
		t.Fatal(err)
	}
	workloadServer.waitForStreamsToClose(t)
}

func TestWorkloadAPIConfigurationRequiresOneCanonicalUnixSocket(t *testing.T) {
	for name, config := range map[string]environment_runtime_transport.Config{
		"valid URI":           {WorkloadAPISocket: "unix:///run/agentplane/workload.sock"},
		"missing URI slashes": {WorkloadAPISocket: "unix:/run/agentplane/workload.sock"},
		"relative URI path":   {WorkloadAPISocket: "unix://relative/workload.sock"},
		"non-Unix URI":        {WorkloadAPISocket: "tcp:///run/agentplane/workload.sock"},
		"empty query":         {WorkloadAPISocket: "unix:///run/agentplane/workload.sock?"},
		"empty fragment":      {WorkloadAPISocket: "unix:///run/agentplane/workload.sock#"},
		"mixed file and workload sources": {
			CertFile: "/run/tls.crt", KeyFile: "/run/tls.key", CAFile: "/run/ca.crt",
			WorkloadAPISocket: "unix:///run/agentplane/workload.sock",
		},
	} {
		t.Run(name, func(t *testing.T) {
			err := config.Validate()
			if name == "valid URI" {
				if err != nil {
					t.Fatalf("Validate() rejected valid socket: %v", err)
				}
			} else if err == nil {
				t.Fatalf("Validate() accepted invalid config: %+v", config)
			}
		})
	}
	config := environment_runtime_transport.Security{
		Mode: environment_runtime_transport.MutualTLS,
		TLS:  &environment_runtime_transport.Config{WorkloadAPISocket: "unix:///run/agentplane/workload.sock"},
	}
	if _, err := config.TLS.ServerCredentials(environment_runtime_transport.GatewayURI); err == nil || !strings.Contains(err.Error(), "Security.Open(ctx)") {
		t.Fatalf("file-only Config.ServerCredentials() error = %v, want explicit Open(ctx) error", err)
	}
}

func waitForWorkloadHandshake(t *testing.T, client, server credentials.TransportCredentials, want []byte) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for {
		clientInfo, _, err := handshake(t, client, server)
		if err != nil {
			t.Fatalf("handshake during SVID rotation: %v", err)
		}
		if got := peerLeaf(t, clientInfo); string(got.Raw) == string(want) {
			return
		}
		if time.Now().After(deadline) {
			t.Fatal("Workload API source did not rotate the presented certificate")
		}
		time.Sleep(10 * time.Millisecond)
	}
}

type workloadAPITestServer struct {
	workload.UnimplementedSpiffeWorkloadAPIServer

	endpoint    string
	initial     *workload.X509SVID
	bundle      []byte
	updates     chan *workload.X509SVID
	streamStart chan string
	streamDone  chan struct{}
}

func newWorkloadAPITestServer(t *testing.T, initial *workload.X509SVID, bundle []byte) *workloadAPITestServer {
	t.Helper()
	path := filepath.Join(t.TempDir(), "workload.sock")
	listener, err := net.Listen("unix", path)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	api := &workloadAPITestServer{
		endpoint: "unix://" + path, initial: initial, bundle: bundle,
		updates: make(chan *workload.X509SVID, 4), streamStart: make(chan string, 2),
		streamDone: make(chan struct{}, 2),
	}
	workload.RegisterSpiffeWorkloadAPIServer(server, api)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return api
}

func (s *workloadAPITestServer) FetchX509SVID(_ *workload.X509SVIDRequest, stream workload.SpiffeWorkloadAPI_FetchX509SVIDServer) error {
	defer func() { s.streamDone <- struct{}{} }()
	if err := requireWorkloadAPIMetadata(stream.Context()); err != nil {
		return err
	}
	s.streamStart <- "svid"
	if err := stream.Send(&workload.X509SVIDResponse{Svids: []*workload.X509SVID{s.initial}}); err != nil {
		return err
	}
	for {
		select {
		case update := <-s.updates:
			if err := stream.Send(&workload.X509SVIDResponse{Svids: []*workload.X509SVID{update}}); err != nil {
				return err
			}
		case <-stream.Context().Done():
			return stream.Context().Err()
		}
	}
}

func (s *workloadAPITestServer) FetchX509Bundles(_ *workload.X509BundlesRequest, stream workload.SpiffeWorkloadAPI_FetchX509BundlesServer) error {
	defer func() { s.streamDone <- struct{}{} }()
	if err := requireWorkloadAPIMetadata(stream.Context()); err != nil {
		return err
	}
	s.streamStart <- "bundles"
	if err := stream.Send(&workload.X509BundlesResponse{Bundles: map[string][]byte{"spiffe://agentplane": s.bundle}}); err != nil {
		return err
	}
	<-stream.Context().Done()
	return stream.Context().Err()
}

func requireWorkloadAPIMetadata(ctx context.Context) error {
	md, ok := metadata.FromIncomingContext(ctx)
	if !ok || len(md.Get("workload.spiffe.io")) != 1 || md.Get("workload.spiffe.io")[0] != "true" {
		return status.Error(codes.PermissionDenied, "workload.spiffe.io: true is required")
	}
	return nil
}

func (s *workloadAPITestServer) waitForStreamsToClose(t *testing.T) {
	t.Helper()
	seen := map[string]bool{}
	quiet := time.NewTimer(100 * time.Millisecond)
	defer quiet.Stop()
	for {
		select {
		case stream := <-s.streamStart:
			seen[stream] = true
			if !quiet.Stop() {
				select {
				case <-quiet.C:
				default:
				}
			}
			quiet.Reset(100 * time.Millisecond)
		case <-quiet.C:
			if !seen["svid"] {
				t.Fatal("Workload API source did not open the X.509 SVID stream")
			}
			goto streamsStarted
		}
	}
streamsStarted:
	for range seen {
		select {
		case <-s.streamDone:
		case <-time.After(time.Second):
			t.Fatal("closing opened security left a Workload API stream running")
		}
	}
}

func workloadX509SVID(t *testing.T, config environment_runtime_transport.Config, identity string, bundle []byte) *workload.X509SVID {
	t.Helper()
	certificatePEM, err := os.ReadFile(config.CertFile)
	if err != nil {
		t.Fatal(err)
	}
	var certificates []byte
	for len(certificatePEM) != 0 {
		block, rest := pem.Decode(certificatePEM)
		if block == nil {
			break
		}
		certificates = append(certificates, block.Bytes...)
		certificatePEM = rest
	}
	keyPair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	privateKey, err := x509.MarshalPKCS8PrivateKey(keyPair.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	return &workload.X509SVID{
		SpiffeId: identity, X509Svid: certificates, X509SvidKey: privateKey, Bundle: bundle,
	}
}
