package rpc

import (
	"bytes"
	"context"
	"os"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sampleradminpb "github.com/agentydragon/ducktape/agentplane/sampler/api/sampleradminpb"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionconfig"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore"
	samplersettings "github.com/agentydragon/ducktape/agentplane/sampler/internal/settings"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/testsupport"
	"github.com/agentydragon/ducktape/agentplane/sampler/migrations"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
	"github.com/agentydragon/ducktape/agentplane/testing/postgres"
	"github.com/google/uuid"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type requestReadStream struct {
	grpc.ServerStream
	ctx       context.Context
	responses []*sampleradminpb.ReadProviderRequestResponse
}

var rpcIntegrationDatabase *postgres.Database

func TestMain(m *testing.M) {
	os.Exit(postgres.RunIntegrationTestsWithDatabase(m, migrations.Up, &rpcIntegrationDatabase))
}

func newTestStore(t testing.TB) *sessionstore.Store {
	t.Helper()
	dsn := rpcIntegrationDatabase.Clone(t)
	db := postgres.Open(t, dsn)
	return sessionstore.New(db, 30*time.Second)
}

func openStoreSession(t *testing.T, store *sessionstore.Store, request *sessionconfig.Configuration, ownerUID string, api protocol.APIStyle) (*sessionstore.Session, error) {
	t.Helper()
	testsupport.FillSessionConfigurationDefaults(request)
	platformInstructions := "Fixture Agentplane platform guidance."
	return store.CreateSession(t.Context(), request, ownerUID, api, &platformInstructions)
}

func providerAttemptTestPolicyPointer() *sessionstore.ProviderAttemptPolicy {
	return &sessionstore.ProviderAttemptPolicy{Budget: 3, Lease: time.Minute}
}

func (s *requestReadStream) Context() context.Context { return s.ctx }

func (s *requestReadStream) Send(response *sampleradminpb.ReadProviderRequestResponse) error {
	s.responses = append(s.responses, response)
	return nil
}

func TestReadProviderRequestStreamsAuthorizedContentAndHidesCrossOwnerRequests(t *testing.T) {
	store := newTestStore(t)
	bodyText := strings.Repeat("provider request body ", maxProviderRequestBodyChunkSize/20+100)
	launch := &sessionconfig.Configuration{
		SessionId: uuid.NewString(),
		Spec:      &sessionpb.SessionSpec{Cwd: "/fixture", Model: "fixture-model", Instructions: bodyText},
	}
	session, err := openStoreSession(t, store, launch, peertls.SessionsURI, protocol.APIStyleResponses)
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
	preparation := &providerrequest.Preparation{
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
	stored, err := store.LoadProviderRequest(t.Context(), session.ID, peertls.SessionsURI, requestID)
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
	settings.AdminServiceCallers = []string{peertls.SessionsURI, peertls.ActionsURI}
	sampler := &SamplerAdmin{Sampler: Sampler{Store: store, Settings: settings}}
	stream := &requestReadStream{ctx: serviceContext(t, peertls.SessionsURI)}
	request := &sampleradminpb.ReadProviderRequestRequest{SessionId: session.ID, RequestId: requestID}
	if err := sampler.ReadProviderRequest(request, stream); err != nil {
		t.Fatal(err)
	}
	if len(stream.responses) < 3 {
		t.Fatalf("stream had %d responses; want metadata and multiple body chunks", len(stream.responses))
	}
	metadataResponse, ok := stream.responses[0].GetItem().(*sampleradminpb.ReadProviderRequestResponse_Metadata)
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
		chunk, ok := response.GetItem().(*sampleradminpb.ReadProviderRequestResponse_BodyChunk)
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

	otherSampler := &SamplerAdmin{Sampler: Sampler{Store: store, Settings: settings}}
	denied := &requestReadStream{ctx: serviceContext(t, peertls.ActionsURI)}
	err = otherSampler.ReadProviderRequest(request, denied)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("cross-owner read code=%v, want NOT_FOUND", status.Code(err))
	}
	if len(denied.responses) != 0 {
		t.Fatalf("cross-owner read returned %d response(s)", len(denied.responses))
	}

	wrongSession := &requestReadStream{ctx: serviceContext(t, peertls.SessionsURI)}
	err = sampler.ReadProviderRequest(&sampleradminpb.ReadProviderRequestRequest{SessionId: "not-owned-session", RequestId: requestID}, wrongSession)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("wrong-session read code=%v, want NOT_FOUND", status.Code(err))
	}
	if len(wrongSession.responses) != 0 {
		t.Fatalf("wrong-session read returned %d response(s)", len(wrongSession.responses))
	}

	missing := &requestReadStream{ctx: serviceContext(t, peertls.SessionsURI)}
	err = sampler.ReadProviderRequest(&sampleradminpb.ReadProviderRequestRequest{SessionId: session.ID, RequestId: "missing-request"}, missing)
	if status.Code(err) != codes.NotFound {
		t.Fatalf("missing request code=%v, want NOT_FOUND", status.Code(err))
	}
	if len(missing.responses) != 0 {
		t.Fatalf("missing request returned %d response(s)", len(missing.responses))
	}
}
