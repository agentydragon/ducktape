package rpc

import (
	"bytes"
	"context"
	"database/sql"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/internal/sessionstore"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/migrations"

	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/api/samplerpb"
	samplersettings "github.com/agentydragon/ducktape/agentplane/central_sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	_ "github.com/lib/pq"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

type requestReadStream struct {
	grpc.ServerStream
	ctx       context.Context
	responses []*samplerpb.ReadProviderRequestResponse
}

var rpcIntegrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	migrate := func(ctx context.Context, db *sql.DB) error { return migrations.Up(ctx, db) }
	os.Exit(postgres.RunIntegrationTests(migrate, func(database *postgres.Database) int {
		rpcIntegrationDatabase = database
		return m.Run()
	}))
}

func newTestStore(t testing.TB) *sessionstore.Store {
	t.Helper()
	dsn := rpcIntegrationDatabase.Clone(t)
	db, err := sql.Open("postgres", dsn)
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = db.Close() })
	return sessionstore.New(db, 30*time.Second)
}

func openStoreSession(t *testing.T, store *sessionstore.Store, request *samplerpb.CreateSessionRequest, ownerUID string, api protocol.APIStyle) (*sessionstore.Session, error) {
	t.Helper()
	if request.GetExecutionIdentity() == nil {
		request.ExecutionIdentity = &identitypb.Identity{Name: "fixture-session-identity", Uid: "fixture-session-identity-uid"}
	}
	if request.GetExecutor() == nil {
		request.Executor = &identitypb.SandboxDestination{
			Owner:  &identitypb.ServiceAccount{Namespace: "fixture", Name: "executor"},
			Target: &identitypb.EnvironmentTarget{Namespace: "fixture", Name: "fixture-sandbox", Uid: "fixture-sandbox-uid"},
		}
	}
	if request.GetSpec() != nil && request.GetSpec().GetPlacement() == nil {
		request.Spec.Placement = &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{
			ExecutorEnvironment: &sessionpb.ExactEnvironment{Name: request.GetExecutor().GetTarget().GetName(), Uid: request.GetExecutor().GetTarget().GetUid()},
		}}}
	}
	platformInstructions := "Fixture Agentplane platform guidance."
	return store.CreateSession(t.Context(), request, ownerUID, api, &platformInstructions)
}

func providerAttemptTestPolicyPointer() *sessionstore.ProviderAttemptPolicy {
	return &sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: time.Minute}
}

func (s *requestReadStream) Context() context.Context { return s.ctx }

func (s *requestReadStream) Send(response *samplerpb.ReadProviderRequestResponse) error {
	s.responses = append(s.responses, response)
	return nil
}

func TestReadProviderRequestStreamsAuthorizedContentAndHidesCrossAccountRequests(t *testing.T) {
	store := newTestStore(t)
	bodyText := strings.Repeat("provider request body ", maxProviderRequestBodyChunkSize/20+100)
	launch := &samplerpb.CreateSessionRequest{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model", Instructions: bodyText},
	}
	session, err := openStoreSession(t, store, launch, "fixture-account", protocol.APIStyleResponses)
	if err != nil {
		t.Fatal(err)
	}
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.Release(context.Background(), owner) }()

	config := samplersettings.ModelConfig{
		Connection: "fixture", ProviderModel: "fixture-model", API: protocol.APIStyleResponses,
		Responses: responses.Config{Dialect: responses.DialectLite}, ExecEncoding: protocol.ExecEncodingJSON,
	}
	connection := samplersettings.ConnectionConfig{BaseURL: "https://provider.example.test"}
	intent := sessionstore.ProviderRequestIntent(sessionstore.PendingIntentSampling, connection, config)
	preparation := &sessionstore.ProviderRequestPreparation{
		ModelConfig: config, SessionEffort: session.Effort,
		SessionConfiguration: append([]byte(nil), session.Configuration...),
	}
	// The retained provider headers are normally unauthenticated. Exercise filtering against old
	// or malformed retained records that still contain credential headers.
	intent.ProviderRequest.Headers["Authorization"] = "Bearer do-not-return"
	intent.ProviderRequest.Headers["X-API-Key"] = "do-not-return"
	requestID, wire, err := store.Begin(t.Context(), owner, intent, preparation, providerAttemptTestPolicyPointer())
	if err != nil {
		t.Fatal(err)
	}
	stored, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", requestID)
	if err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(stored.Body, wire) {
		t.Fatal("stored request body differs from prepared request bytes")
	}
	if len(stored.Body) <= maxProviderRequestBodyChunkSize {
		t.Fatalf("fixture body is %d bytes; want more than one chunk", len(stored.Body))
	}

	settings := fixtureSettings()
	settings.AllowedAccounts = append(settings.AllowedAccounts, "test/other")
	resolverFor := func(uid, account string) *auth.Resolver {
		t.Helper()
		resolver, err := auth.NewResolver(fixtureReviews{uid: uid, account: account}, "test-sampler", []string{"test"})
		if err != nil {
			t.Fatal(err)
		}
		return resolver
	}
	sampler := &Sampler{Store: store, Resolver: resolverFor("fixture-account", "system:serviceaccount:test:client"), Settings: settings}
	stream := &requestReadStream{ctx: metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer test-token"))}
	request := &samplerpb.ReadProviderRequestRequest{SessionId: session.ID, RequestId: requestID}
	if err := sampler.ReadProviderRequest(request, stream); err != nil {
		t.Fatal(err)
	}
	if len(stream.responses) < 3 {
		t.Fatalf("stream had %d responses; want metadata and multiple body chunks", len(stream.responses))
	}
	metadataResponse, ok := stream.responses[0].GetItem().(*samplerpb.ReadProviderRequestResponse_Metadata)
	if !ok {
		t.Fatalf("first response item is %T, want metadata", stream.responses[0].GetItem())
	}
	gotMetadata := metadataResponse.Metadata
	if gotMetadata.GetRequestId() != stored.RequestID || gotMetadata.GetMethod() != stored.Method ||
		gotMetadata.GetUrl() != stored.URL {
		t.Fatalf("metadata=%v, stored request=%+v", gotMetadata, stored)
	}
	for name := range gotMetadata.GetHeaders() {
		switch strings.ToLower(name) {
		case "authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token":
			t.Fatalf("authentication header %q was returned", name)
		}
	}
	for name, value := range stored.Headers {
		switch strings.ToLower(name) {
		case "authorization", "proxy-authorization", "x-api-key", "api-key", "x-auth-token", "x-access-token":
			continue
		}
		if gotMetadata.GetHeaders()[name] != value {
			t.Fatalf("header %q=%q, want %q", name, gotMetadata.GetHeaders()[name], value)
		}
	}
	var streamedBody []byte
	for _, response := range stream.responses[1:] {
		chunk, ok := response.GetItem().(*samplerpb.ReadProviderRequestResponse_BodyChunk)
		if !ok {
			t.Fatalf("body response item is %T, want body chunk", response.GetItem())
		}
		if len(chunk.BodyChunk) > maxProviderRequestBodyChunkSize {
			t.Fatalf("body chunk is %d bytes, maximum is %d", len(chunk.BodyChunk), maxProviderRequestBodyChunkSize)
		}
		streamedBody = append(streamedBody, chunk.BodyChunk...)
	}
	if !bytes.Equal(streamedBody, stored.Body) {
		t.Fatalf("streamed body has %d bytes; stored body has %d", len(streamedBody), len(stored.Body))
	}

	otherSampler := &Sampler{Store: store, Resolver: resolverFor("other-account", "system:serviceaccount:test:other"), Settings: settings}
	denied := &requestReadStream{ctx: metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer test-token"))}
	err = otherSampler.ReadProviderRequest(request, denied)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("cross-account read code=%v, want NOT_FOUND", status.Code(err))
	}
	if len(denied.responses) != 0 {
		t.Fatalf("cross-account read returned %d response(s)", len(denied.responses))
	}

	wrongSession := &requestReadStream{ctx: metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer test-token"))}
	err = sampler.ReadProviderRequest(&samplerpb.ReadProviderRequestRequest{SessionId: "not-owned-session", RequestId: requestID}, wrongSession)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("wrong-session read code=%v, want NOT_FOUND", status.Code(err))
	}
	if len(wrongSession.responses) != 0 {
		t.Fatalf("wrong-session read returned %d response(s)", len(wrongSession.responses))
	}

	missing := &requestReadStream{ctx: metadata.NewIncomingContext(t.Context(), metadata.Pairs("authorization", "Bearer test-token"))}
	err = sampler.ReadProviderRequest(&samplerpb.ReadProviderRequestRequest{SessionId: session.ID, RequestId: "missing-request"}, missing)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("missing request code=%v, want NOT_FOUND", status.Code(err))
	}
	if len(missing.responses) != 0 {
		t.Fatalf("missing request returned %d response(s)", len(missing.responses))
	}
}
