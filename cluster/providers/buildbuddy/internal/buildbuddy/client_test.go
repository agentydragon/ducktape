package buildbuddy

import (
	"context"
	"crypto/rand"
	"encoding/base64"
	"net"
	"sync"
	"testing"

	apikeypb "github.com/buildbuddy-io/buildbuddy/proto/api_key"
	servicepb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	capabilitypb "github.com/buildbuddy-io/buildbuddy/proto/capability"
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
	apiKeys         map[string]*apikeypb.ApiKey
	createError     codes.Code
	commitBeforeErr bool
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

func (s *testServer) GetApiKeys(ctx context.Context, req *apikeypb.GetApiKeysRequest) (*apikeypb.GetApiKeysResponse, error) {
	s.checkAuth(ctx)
	if req.GetRequestContext().GetGroupId() != "fake-group" || req.GetGroupId() != "fake-group" {
		s.t.Error("API-key list did not use the explicit group")
		return nil, status.Error(codes.InvalidArgument, "unexpected group")
	}
	s.mu.Lock()
	defer s.mu.Unlock()
	response := &apikeypb.GetApiKeysResponse{}
	for _, key := range s.apiKeys {
		// The server's group-list endpoint returns metadata only.
		response.ApiKey = append(response.ApiKey, &apikeypb.ApiKey{
			Id:                  key.GetId(),
			Label:               key.GetLabel(),
			Capability:          append([]capabilitypb.Capability(nil), key.GetCapability()...),
			VisibleToDevelopers: key.GetVisibleToDevelopers(),
		})
	}
	return response, nil
}

func (s *testServer) CreateApiKey(ctx context.Context, req *apikeypb.CreateApiKeyRequest) (*apikeypb.CreateApiKeyResponse, error) {
	s.checkAuth(ctx)
	if req.GetRequestContext().GetGroupId() != "fake-group" || req.GetUserId() != "" {
		s.t.Error("API-key create was not scoped to a group-owned key")
		return nil, status.Error(codes.InvalidArgument, "unexpected group key request")
	}
	if s.createError != codes.OK && !s.commitBeforeErr {
		return nil, status.Error(s.createError, "fake server error")
	}
	s.mu.Lock()
	if s.apiKeys == nil {
		s.apiKeys = make(map[string]*apikeypb.ApiKey)
	}
	key := &apikeypb.ApiKey{
		Id:                  "fake-api-key-id",
		Value:               "fake-generated-key",
		Label:               req.GetLabel(),
		Capability:          append([]capabilitypb.Capability(nil), req.GetCapability()...),
		VisibleToDevelopers: req.GetVisibleToDevelopers(),
	}
	s.apiKeys[key.GetId()] = key
	s.mu.Unlock()
	if s.createError != codes.OK {
		return nil, status.Error(s.createError, "fake response lost")
	}
	return &apikeypb.CreateApiKeyResponse{ApiKey: key}, nil
}

func (s *testServer) UpdateApiKey(ctx context.Context, req *apikeypb.UpdateApiKeyRequest) (*apikeypb.UpdateApiKeyResponse, error) {
	s.checkAuth(ctx)
	s.mu.Lock()
	defer s.mu.Unlock()
	key := s.apiKeys[req.GetId()]
	if key == nil {
		return nil, status.Error(codes.NotFound, "absent")
	}
	key.Label = req.GetLabel()
	key.Capability = append([]capabilitypb.Capability(nil), req.GetCapability()...)
	key.VisibleToDevelopers = req.GetVisibleToDevelopers()
	return &apikeypb.UpdateApiKeyResponse{}, nil
}

func (s *testServer) DeleteApiKey(ctx context.Context, req *apikeypb.DeleteApiKeyRequest) (*apikeypb.DeleteApiKeyResponse, error) {
	s.checkAuth(ctx)
	s.mu.Lock()
	defer s.mu.Unlock()
	if s.apiKeys[req.GetId()] == nil {
		return nil, status.Error(codes.NotFound, "absent")
	}
	delete(s.apiKeys, req.GetId())
	return &apikeypb.DeleteApiKeyResponse{}, nil
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

func TestAPIKeyLifecycle(t *testing.T) {
	impl := &testServer{t: t, apiKeys: make(map[string]*apikeypb.ApiKey)}
	c := newTestClient(t, impl)
	ctx := context.Background()
	created, err := c.CreateAPIKey(ctx, "fake-group", "fake-label", []string{"CAS_WRITE", "CACHE_WRITE"}, true)
	if err != nil {
		t.Fatal("create API key:", err)
	}
	if created.ID != "fake-api-key-id" || string(created.Value) != "fake-generated-key" {
		t.Fatal("create returned incomplete API-key material")
	}
	if len(created.Capabilities) != 2 || created.Capabilities[0] != "CACHE_WRITE" || created.Capabilities[1] != "CAS_WRITE" {
		t.Fatal("create returned unexpected API-key capabilities")
	}

	keys, err := c.APIKeys(ctx, "fake-group")
	if err != nil || len(keys) != 1 {
		t.Fatalf("list API keys: got %d keys, err=%v", len(keys), err)
	}
	if keys[0].ID != created.ID || keys[0].Label != "fake-label" || len(keys[0].Capabilities) != 2 || keys[0].Capabilities[0] != "CACHE_WRITE" {
		t.Fatal("list returned unexpected API-key metadata")
	}
	if err := c.UpdateAPIKey(ctx, created.ID, "updated-label", []string{"ORG_ADMIN"}, false); err != nil {
		t.Fatal("update API key:", err)
	}
	keys, err = c.APIKeys(ctx, "fake-group")
	if err != nil || len(keys) != 1 || keys[0].Label != "updated-label" || len(keys[0].Capabilities) != 1 || keys[0].Capabilities[0] != "ORG_ADMIN" || keys[0].VisibleToDevelopers {
		t.Fatal("API-key update was not reflected by group listing")
	}
	if err := c.UpdateAPIKey(ctx, created.ID, "read-only-key", nil, false); err != nil {
		t.Fatal("clear API-key capabilities:", err)
	}
	keys, err = c.APIKeys(ctx, "fake-group")
	if err != nil || len(keys) != 1 || keys[0].Label != "read-only-key" || len(keys[0].Capabilities) != 0 {
		t.Fatal("clearing API-key capabilities was not reflected by group listing")
	}
	if err := c.DeleteAPIKey(ctx, created.ID); err != nil {
		t.Fatal("delete API key:", err)
	}
	if err := c.DeleteAPIKey(ctx, created.ID); err != nil {
		t.Fatal("repeated API-key delete must be idempotent:", err)
	}
	readOnly, err := c.CreateAPIKey(ctx, "fake-group", "read-only-key", nil, false)
	if err != nil {
		t.Fatal("create API key without capabilities:", err)
	}
	if len(readOnly.Capabilities) != 0 {
		t.Fatalf("created read-only API key has capabilities: %v", readOnly.Capabilities)
	}
}

func TestAPIKeyCreateAmbiguity(t *testing.T) {
	impl := &testServer{t: t, apiKeys: make(map[string]*apikeypb.ApiKey), createError: codes.Unavailable, commitBeforeErr: true}
	c := newTestClient(t, impl)
	_, err := c.CreateAPIKey(context.Background(), "fake-group", "fake-label", []string{"CACHE_WRITE"}, false)
	if err == nil || !IsAmbiguousAPIKeyCreate(err) {
		t.Fatal("a lost response after key creation must be classified as ambiguous")
	}
	if len(impl.apiKeys) != 1 {
		t.Fatal("test server did not retain the ambiguous created key")
	}

	impl = &testServer{t: t, apiKeys: make(map[string]*apikeypb.ApiKey), createError: codes.PermissionDenied}
	c = newTestClient(t, impl)
	_, err = c.CreateAPIKey(context.Background(), "fake-group", "fake-label", []string{"CACHE_WRITE"}, false)
	if err == nil || IsAmbiguousAPIKeyCreate(err) {
		t.Fatal("a permission rejection must be safe to retry")
	}
	if len(impl.apiKeys) != 0 {
		t.Fatal("permission rejection unexpectedly created a key")
	}
}

func newTestClient(t *testing.T, impl *testServer) *Client {
	t.Helper()
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
	return c
}

func TestRejectsInvalidPublicKey(t *testing.T) {
	c := Client{}
	if err := c.Put(context.Background(), "FAKE", []byte("fake"), PublicKey{Value: "bad"}); err == nil {
		t.Fatal("expected invalid public key")
	}
}
