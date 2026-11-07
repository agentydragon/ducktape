package workloadidentity

import (
	"bytes"
	"context"
	"crypto/tls"
	"crypto/x509"
	"encoding/pem"
	"net"
	"os"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/environment_runtime_transport"
	environmentruntimetransporttesting "github.com/agentydragon/ducktape/agentplane/environment_runtime_transport/testing"
	"github.com/spiffe/go-spiffe/v2/proto/spiffe/workload"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/proto"
)

func TestFetchX509SVIDStreamsRotationsAndReconnectStartsWithFullState(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	first := parseFixture(t, ca, issueRuntime(t, ca, identity))
	second := parseFixture(t, ca, issueRuntime(t, ca, identity))
	server := New()
	if err := server.Set(first); err != nil {
		t.Fatal(err)
	}
	connection := startWorkloadServer(t, server)
	client := workload.NewSpiffeWorkloadAPIClient(connection)

	stream, cancel := fetchX509SVID(t, client)
	received, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(received, first) {
		t.Fatalf("initial stream response = %v, want complete first snapshot", received)
	}
	if err := server.Set(second); err != nil {
		t.Fatal(err)
	}
	rotated, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(rotated, second) {
		t.Fatalf("rotation response = %v, want complete second snapshot", rotated)
	}
	cancel()

	reconnected, cancel := fetchX509SVID(t, client)
	defer cancel()
	latest, err := reconnected.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(latest, second) {
		t.Fatalf("reconnect response = %v, want complete latest snapshot", latest)
	}
}

func TestSetCoalescesPendingSubscriberUpdates(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	first := parseFixture(t, ca, issueRuntime(t, ca, identity))
	second := parseFixture(t, ca, issueRuntime(t, ca, identity))
	third := parseFixture(t, ca, issueRuntime(t, ca, identity))
	server := New()
	if err := server.Set(first); err != nil {
		t.Fatal(err)
	}
	updates, unsubscribe, err := server.subscribe()
	if err != nil {
		t.Fatal(err)
	}
	defer unsubscribe()
	if got := <-updates; !proto.Equal(got, first) {
		t.Fatalf("initial update = %v, want first snapshot", got)
	}
	if err := server.Set(second); err != nil {
		t.Fatal(err)
	}
	if err := server.Set(third); err != nil {
		t.Fatal(err)
	}
	if got := <-updates; !proto.Equal(got, third) {
		t.Fatalf("coalesced update = %v, want latest complete snapshot", got)
	}
}

func TestFetchX509BundlesRequiresMetadataAndReturnsLocalBundle(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	response := parseFixture(t, ca, issueRuntime(t, ca, identity))
	server := New()
	if err := server.Set(response); err != nil {
		t.Fatal(err)
	}
	connection := startWorkloadServer(t, server)
	client := workload.NewSpiffeWorkloadAPIClient(connection)

	ctx, cancel := context.WithCancel(metadata.NewOutgoingContext(context.Background(), metadata.Pairs(workloadMetadataKey, "true")))
	defer cancel()
	stream, err := client.FetchX509Bundles(ctx, &workload.X509BundlesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	bundles, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if got := bundles.GetBundles()["spiffe://agentplane"]; !bytes.Equal(got, response.GetSvids()[0].GetBundle()) {
		t.Fatal("bundle response did not contain the local trust-domain bundle")
	}

	missingMetadataStream, err := client.FetchX509Bundles(context.Background(), &workload.X509BundlesRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := missingMetadataStream.Recv(); status.Code(err) != codes.InvalidArgument {
		t.Fatalf("missing metadata error = %v, want InvalidArgument", err)
	}
}

func TestFetchX509SVIDRejectsMissingOrInvalidMetadata(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	server := New()
	if err := server.Set(parseFixture(t, ca, issueRuntime(t, ca, identity))); err != nil {
		t.Fatal(err)
	}
	connection := startWorkloadServer(t, server)
	client := workload.NewSpiffeWorkloadAPIClient(connection)

	for _, values := range [][]string{nil, {"false"}, {"true", "true"}} {
		ctx := context.Background()
		if values != nil {
			ctx = metadata.NewOutgoingContext(ctx, metadata.MD{workloadMetadataKey: values})
		}
		stream, err := client.FetchX509SVID(ctx, &workload.X509SVIDRequest{})
		if err != nil {
			t.Fatal(err)
		}
		if _, err := stream.Recv(); status.Code(err) != codes.InvalidArgument {
			t.Fatalf("metadata values %v error = %v, want InvalidArgument", values, err)
		}
	}
}

func TestParsePEMAndSetRejectMismatchedBundleAndIdentity(t *testing.T) {
	trustedCA := environmentruntimetransporttesting.NewCA(t)
	otherCA := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	config := issueRuntime(t, trustedCA, identity)
	certPEM := readFile(t, config.CertFile)
	keyPEM := readFile(t, config.KeyFile)
	wrongBundle := readFile(t, otherCA.RootFile)
	if _, err := ParsePEM(certPEM, keyPEM, wrongBundle); err == nil {
		t.Fatal("ParsePEM accepted a certificate chain with an unrelated bundle")
	}

	first := parseFixture(t, trustedCA, config)
	secondIdentity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/other-credential"
	mismatchedIdentity := parseFixture(t, trustedCA, issueRuntime(t, trustedCA, secondIdentity))
	mismatchedIdentity.GetSvids()[0].SpiffeId = first.GetSvids()[0].GetSpiffeId()
	server := New()
	if err := server.Set(first); err != nil {
		t.Fatal(err)
	}
	if err := server.Set(mismatchedIdentity); err == nil {
		t.Fatal("Set accepted a response SPIFFE ID that disagrees with its certificate")
	}
	snapshot, err := server.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(snapshot, first) {
		t.Fatal("rejected identity replacement changed the active snapshot")
	}

	if err := server.Set(parseFixture(t, trustedCA, issueRuntime(t, trustedCA, secondIdentity))); err == nil {
		t.Fatal("Set accepted a new SPIFFE ID after pinning the first identity")
	}
}

func TestParsePEMRequiresRuntimeServerAuthUsage(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	config := ca.IssueWithUsage(t, identity, x509.ExtKeyUsageClientAuth)
	if _, err := ParsePEM(readFile(t, config.CertFile), readFile(t, config.KeyFile), readFile(t, ca.RootFile)); err == nil {
		t.Fatal("ParsePEM accepted a Runtime certificate without serverAuth EKU")
	}
}

func TestParsePEMNormalizesPKCS8AndRejectsMismatchedKeys(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	config := issueRuntime(t, ca, identity)
	want := parseFixture(t, ca, config) // The cert-manager-style fixture uses SEC1 EC PEM.
	pair, err := tls.LoadX509KeyPair(config.CertFile, config.KeyFile)
	if err != nil {
		t.Fatal(err)
	}
	keyDER, err := x509.MarshalPKCS8PrivateKey(pair.PrivateKey)
	if err != nil {
		t.Fatal(err)
	}
	keyPEM := pem.EncodeToMemory(&pem.Block{Type: "PRIVATE KEY", Bytes: keyDER})
	got, err := ParsePEM(readFile(t, config.CertFile), keyPEM, readFile(t, ca.RootFile))
	if err != nil {
		t.Fatal(err)
	}
	if !proto.Equal(got, want) {
		t.Fatal("SEC1 and PKCS8 inputs produced different Workload API credentials")
	}
	other := issueRuntime(t, ca, identity)
	if _, err := ParsePEM(readFile(t, config.CertFile), readFile(t, other.KeyFile), readFile(t, ca.RootFile)); err == nil {
		t.Fatal("ParsePEM accepted a private key that does not match the certificate")
	}
}

func TestSnapshotIsDetachedAndRejectsOversizedPayload(t *testing.T) {
	ca := environmentruntimetransporttesting.NewCA(t)
	identity := "spiffe://agentplane/environment-runtime/agent-ns/sandbox-uid/credential-id"
	response := parseFixture(t, ca, issueRuntime(t, ca, identity))
	server := New()
	if err := server.Set(response); err != nil {
		t.Fatal(err)
	}
	response.GetSvids()[0].X509Svid[0] ^= 0xff
	snapshot, err := server.Snapshot()
	if err != nil {
		t.Fatal(err)
	}
	if bytes.Equal(snapshot.GetSvids()[0].GetX509Svid(), response.GetSvids()[0].GetX509Svid()) {
		t.Fatal("Snapshot aliases the caller's response")
	}

	oversized := proto.Clone(snapshot).(*workload.X509SVIDResponse)
	oversized.Crl = [][]byte{bytes.Repeat([]byte("x"), maxPayloadBytes)}
	if err := server.Set(oversized); err == nil {
		t.Fatal("Set accepted a response larger than 1 MiB")
	}
}

func parseFixture(t *testing.T, ca *environmentruntimetransporttesting.CA, config environment_runtime_transport.Config) *workload.X509SVIDResponse {
	t.Helper()
	response, err := ParsePEM(readFile(t, config.CertFile), readFile(t, config.KeyFile), readFile(t, ca.RootFile))
	if err != nil {
		t.Fatal(err)
	}
	return response
}

func issueRuntime(t *testing.T, ca *environmentruntimetransporttesting.CA, identity string) environment_runtime_transport.Config {
	t.Helper()
	return ca.IssueWithUsage(t, identity, x509.ExtKeyUsageServerAuth)
}

func readFile(t *testing.T, path string) []byte {
	t.Helper()
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	return data
}

func startWorkloadServer(t *testing.T, server *Server) *grpc.ClientConn {
	t.Helper()
	listener := bufconn.Listen(1 << 20)
	grpcServer := grpc.NewServer()
	workload.RegisterSpiffeWorkloadAPIServer(grpcServer, server)
	go func() {
		_ = grpcServer.Serve(listener)
	}()
	t.Cleanup(func() {
		grpcServer.Stop()
	})
	connection, err := grpc.NewClient("passthrough:///workload-api", grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
		return listener.Dial()
	}), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() {
		_ = connection.Close()
	})
	return connection
}

func fetchX509SVID(t *testing.T, client workload.SpiffeWorkloadAPIClient) (workload.SpiffeWorkloadAPI_FetchX509SVIDClient, context.CancelFunc) {
	t.Helper()
	ctx, cancel := context.WithCancel(metadata.NewOutgoingContext(context.Background(), metadata.Pairs(workloadMetadataKey, "true")))
	stream, err := client.FetchX509SVID(ctx, &workload.X509SVIDRequest{})
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	return stream, cancel
}
