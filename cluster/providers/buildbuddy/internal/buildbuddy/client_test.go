package buildbuddy

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"net"
	"sync"
	"testing"

	servicepb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	secretspb "github.com/buildbuddy-io/buildbuddy/proto/secrets"
	"golang.org/x/crypto/nacl/box"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
)

type testServer struct {
	servicepb.UnimplementedBuildBuddyServiceServer
	t               *testing.T
	public, private *[32]byte
	mu              sync.Mutex
	stored          []byte
}

func (s *testServer) checkAuth(ctx context.Context) {
	if values := metadata.ValueFromIncomingContext(ctx, "x-buildbuddy-api-key"); len(values) != 1 || values[0] != "fake-admin" {
		s.t.Error("missing API key metadata")
	}
}

func (s *testServer) GetPublicKey(ctx context.Context, _ *secretspb.GetPublicKeyRequest) (*secretspb.GetPublicKeyResponse, error) {
	s.checkAuth(ctx)
	return &secretspb.GetPublicKeyResponse{PublicKey: &secretspb.PublicKey{Id: "fake-key", Value: base64.StdEncoding.EncodeToString(s.public[:])}}, nil
}

func (s *testServer) ListSecrets(ctx context.Context, _ *secretspb.ListSecretsRequest) (*secretspb.ListSecretsResponse, error) {
	s.checkAuth(ctx)
	s.mu.Lock()
	defer s.mu.Unlock()
	response := &secretspb.ListSecretsResponse{}
	if s.stored != nil {
		response.Secret = []*secretspb.Secret{{Name: "FAKE_TEST_KEY"}}
	}
	return response, nil
}

func (s *testServer) UpdateSecret(ctx context.Context, req *secretspb.UpdateSecretRequest) (*secretspb.UpdateSecretResponse, error) {
	s.checkAuth(ctx)
	if req.GetSecret().GetName() != "FAKE_TEST_KEY" {
		s.t.Error("unexpected secret name")
	}
	sealed, err := base64.StdEncoding.DecodeString(req.GetSecret().GetValue())
	if err != nil {
		return nil, err
	}
	plaintext, ok := box.OpenAnonymous(nil, sealed, s.public, s.private)
	if !ok {
		return nil, status.Error(codes.InvalidArgument, "cannot decrypt")
	}
	s.mu.Lock()
	s.stored = plaintext
	s.mu.Unlock()
	return &secretspb.UpdateSecretResponse{}, nil
}

func (s *testServer) DeleteSecret(ctx context.Context, _ *secretspb.DeleteSecretRequest) (*secretspb.DeleteSecretResponse, error) {
	s.checkAuth(ctx)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.stored == nil {
		return nil, status.Error(codes.NotFound, "absent")
	}
	s.stored = nil
	return &secretspb.DeleteSecretResponse{}, nil
}

func TestSecretLifecycle(t *testing.T) {
	public, private, err := box.GenerateKey(rand.Reader)
	if err != nil {
		t.Fatal(err)
	}
	impl := &testServer{t: t, public: public, private: private}
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	servicepb.RegisterBuildBuddyServiceServer(server, impl)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() { server.Stop(); _ = listener.Close() })
	conn, err := grpc.NewClient("passthrough:///bufnet", grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	c := &Client{rpc: servicepb.NewBuildBuddyServiceClient(conn), conn: conn, apiKey: "fake-admin"}
	t.Cleanup(func() { _ = c.Close() })
	ctx := context.Background()
	key, err := c.PublicKey(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Put(ctx, "FAKE_TEST_KEY", []byte("fake-value"), key); err != nil {
		t.Fatal(err)
	}
	impl.mu.Lock()
	got := string(impl.stored)
	impl.mu.Unlock()
	if got != "fake-value" {
		t.Fatal("server received wrong value")
	}
	if exists, err := c.Exists(ctx, "FAKE_TEST_KEY"); err != nil || !exists {
		t.Fatalf("exists=%v err=%v", exists, err)
	}
	if err := c.Delete(ctx, "FAKE_TEST_KEY"); err != nil {
		t.Fatal(err)
	}
	if err := c.Delete(ctx, "FAKE_TEST_KEY"); err != nil {
		t.Fatal("repeated delete must be idempotent", err)
	}
	if exists, err := c.Exists(ctx, "FAKE_TEST_KEY"); err != nil || exists {
		t.Fatalf("exists=%v err=%v", exists, err)
	}
}

func TestRejectsInvalidPublicKey(t *testing.T) {
	c := Client{}
	if err := c.Put(context.Background(), "FAKE", []byte("fake"), PublicKey{Value: "bad"}); err == nil {
		t.Fatal("expected invalid public key")
	}
}
