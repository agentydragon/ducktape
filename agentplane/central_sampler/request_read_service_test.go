package centralsampler

import (
	"bytes"
	"context"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/central_sampler/responses"
	samplerpb "github.com/agentydragon/ducktape/agentplane/central_sampler/samplerpb"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
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

func (s *requestReadStream) Context() context.Context { return s.ctx }

func (s *requestReadStream) Send(response *samplerpb.ReadProviderRequestResponse) error {
	s.responses = append(s.responses, response)
	return nil
}

func TestReadProviderRequestStreamsAuthorizedContentAndHidesCrossAccountRequests(t *testing.T) {
	store := newTestStore(t)
	session := openSessionForStore(t, store, "")
	owner, err := store.Claim(t.Context(), session.ConversationID, time.Minute)
	if err != nil {
		t.Fatal(err)
	}
	defer func() { _ = store.Release(context.Background(), owner) }()

	bodyText := strings.Repeat("provider request body ", maxProviderRequestBodyChunkSize/20+100)
	config := ModelConfig{
		Connection: "fixture", ProviderModel: "fixture-model", API: protocol.APIStyleResponses,
		Responses: responses.Config{Dialect: responses.DialectLite},
	}
	connection := ConnectionConfig{BaseURL: "https://provider.example.test"}
	body := responses.Request{
		Model: "fixture-model", Instructions: bodyText, Input: []responses.RequestInput{}, Stream: true,
	}
	intent := providerRequestIntent(PendingIntentSampling, connection, config)
	// The retained provider headers are normally unauthenticated. Exercise filtering against old
	// or malformed retained records that still contain credential headers.
	intent.ProviderRequest.Headers["Authorization"] = "Bearer do-not-return"
	intent.ProviderRequest.Headers["X-API-Key"] = "do-not-return"
	requestID, wire, err := store.Begin(t.Context(), owner, intent, body)
	if err != nil {
		t.Fatal(err)
	}
	stored, err := store.LoadProviderRequest(t.Context(), session.ID, "fixture-account", requestID)
	if err != nil {
		t.Fatal(err)
	}
	equivalentJSON(t, stored.Body, wire)
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
	if gotMetadata.GetRequestId() != stored.RequestID || gotMetadata.GetContextId() != stored.ContextID ||
		gotMetadata.GetProfileId() != string(stored.ProfileID) || gotMetadata.GetMethod() != stored.Method ||
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
