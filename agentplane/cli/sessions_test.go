package main

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestObserveInputRequiresItsOwnHistoryConfiguration(t *testing.T) {
	env := map[string]string{
		"AGENTPLANE_SESSIONS_URL":        "http://127.0.0.1:1",
		"AGENTPLANE_SESSIONS_CREDENTIAL": "dispatcher-placeholder",
	}
	code, stdout, stderr := invoke(env, "sessions", "observe-input", "58cae82c-453d-4de7-ae5b-17a8f7596077", "--command-id", "c40dd833-326f-4be1-b42a-4bb74c76c49a")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "AGENTPLANE_HISTORY_URL not set") {
		t.Fatalf("exit %d, stdout %q, stderr %q; want independent History configuration error", code, stdout, stderr)
	}
}

func TestObserveInputRejectsWaitOverBoundBeforeConnecting(t *testing.T) {
	code, stdout, stderr := invoke(nil, "sessions", "observe-input", "session", "--command-id", "command", "--wait-seconds", "31")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "--wait-seconds must be between 0 and 30") {
		t.Fatalf("exit %d, stdout %q, stderr %q; want bounded wait validation", code, stdout, stderr)
	}
}

func TestCreateSessionRequiresCanonicalUUIDBeforeConnecting(t *testing.T) {
	for _, sessionID := range []string{"not-a-uuid", "ABCDEFAB-CDEF-4ABC-8DEF-ABCDEFABCDEF", "abcdefabcdef4abcdefabcdefabcdef"} {
		code, stdout, stderr := invoke(nil, "sessions", "create", sessionID, "--sandbox", "sandbox", "--sandbox-uid", "uid", "--spec", "{}")
		if code != exitUsage || stdout != "" || !strings.Contains(stderr, "SESSION_ID must be a canonical UUID") {
			t.Errorf("sessions create %q: exit %d, stdout %q, stderr %q; want canonical UUID usage error", sessionID, code, stdout, stderr)
		}
	}
}

func TestSessionLineRejectsIncompleteResource(t *testing.T) {
	for _, raw := range []string{
		`{"sessionId":"session","placement":{"environmentHosted":{"environment":{"name":"sandbox","uid":"uid"}}}}`,
		`{"sessionId":"session","status":{"materialization":"SESSION_MATERIALIZATION_WAITING"}}`,
	} {
		if _, err := sessionLine(json.RawMessage(raw)); err == nil {
			t.Errorf("sessionLine(%s) accepted an incomplete Session", raw)
		}
	}
}

func TestSessionGetAndReadTurnsExposeNeutralProjections(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fixture := &sessionProjectionFixture{}
	dispatcherpb.RegisterSessionDispatcherServer(server, fixture)
	historypb.RegisterHistoryServiceServer(server, fixture)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	env := map[string]string{
		"AGENTPLANE_SESSIONS_URL":        "http://" + listener.Addr().String(),
		"AGENTPLANE_SESSIONS_CREDENTIAL": testCredential,
		"AGENTPLANE_HISTORY_URL":         "http://" + listener.Addr().String(),
		"AGENTPLANE_HISTORY_CREDENTIAL":  testCredential,
	}
	const sessionID = "58cae82c-453d-4de7-ae5b-17a8f7596077"
	code, stdout, stderr := invoke(env, "--json", "sessions", "get", sessionID)
	if code != 0 || stderr != "" {
		t.Fatalf("sessions get: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
	var snapshot map[string]any
	if err := json.Unmarshal([]byte(stdout), &snapshot); err != nil {
		t.Fatalf("sessions get JSON: %v", err)
	}
	session := snapshot["session"].(map[string]any)
	history := snapshot["history"].(map[string]any)
	if session["kind"] != "observed" || history["kind"] != "observed" {
		t.Fatalf("sessions get source parts = %#v", snapshot)
	}
	delete(env, "AGENTPLANE_SESSIONS_URL")
	delete(env, "AGENTPLANE_SESSIONS_CREDENTIAL")
	code, stdout, stderr = invoke(env, "--json", "sessions", "read-turns", sessionID, "--count", "1")
	if code != 0 || stderr != "" {
		t.Fatalf("sessions read-turns: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
	var page map[string]any
	if err := json.Unmarshal([]byte(stdout), &page); err != nil {
		t.Fatalf("sessions read-turns JSON: %v", err)
	}
	turns := page["turns"].([]any)
	if len(turns) != 1 || turns[0].(map[string]any)["turn_id"] != "provider:turn/opaque" {
		t.Fatalf("sessions read-turns page = %#v", page)
	}
}

type sessionProjectionFixture struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	historypb.UnimplementedHistoryServiceServer
}

func (sessionProjectionFixture) GetSession(_ context.Context, request *dispatcherpb.GetSessionRequest) (*dispatcherpb.GetSessionResponse, error) {
	return &dispatcherpb.GetSessionResponse{Session: &dispatcherpb.Session{
		SessionId: request.GetSessionId(), Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_ACCEPTED},
	}}, nil
}

func (sessionProjectionFixture) GetSessionSummary(_ context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	return &historypb.SessionSummary{SessionId: request.GetSessionId(), FeedStatus: historypb.FeedStatus_FEED_STATUS_ACTIVE}, nil
}

func (sessionProjectionFixture) ReadSessionEvents(_ context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	events := []*eventlogpb.EventEntry{
		{Cursor: 1, Event: &eventpb.Event{Observation: &eventpb.Event_TurnStarted{TurnStarted: &eventpb.TurnStarted{TurnId: "provider:turn/opaque"}}}},
		{Cursor: 2, Event: &eventpb.Event{Observation: &eventpb.Event_TurnCompleted{TurnCompleted: &eventpb.TurnCompleted{TurnId: "provider:turn/opaque", Status: eventpb.TurnStatus_TURN_STATUS_COMPLETED}}}},
	}
	response := &historypb.ReadSessionEventsResponse{LastCursor: 2}
	for _, event := range events {
		if event.GetCursor() > request.GetAfterCursor() && uint32(len(response.Entries)) < request.GetLimit() {
			response.Entries = append(response.Entries, event)
		}
	}
	return response, nil
}

func TestSessionListOmitsAuthorizationMetadataWithoutBearerConfiguration(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	fixture := &sessionListFixture{
		requests:       make(chan *dispatcherpb.ListSessionsRequest, 1),
		authorizations: make(chan []string, 2),
	}
	dispatcherpb.RegisterSessionDispatcherServer(server, fixture)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	env := map[string]string{
		"AGENTPLANE_SESSIONS_URL": "http://" + listener.Addr().String(),
	}
	for _, asJSON := range []bool{true, false} {
		args := []string{"sessions", "list", "--limit", "1", "--after", "previous-page"}
		if asJSON {
			args = append([]string{"--json"}, args...)
		}
		code, stdout, stderr := invoke(env, args...)
		if code != 0 {
			t.Fatalf("sessions list: exit %d, stdout %q, stderr %q", code, stdout, stderr)
		}
		request := <-fixture.requests
		if authorization := <-fixture.authorizations; len(authorization) != 0 {
			t.Fatalf("Authorization metadata = %v; want no bearer when Sessions credential is unset", authorization)
		}
		if request.GetLimit() != 1 || request.GetPageToken() != "previous-page" {
			t.Fatalf("list request: %v", request)
		}
		if asJSON {
			page := &dispatcherpb.ListSessionsResponse{}
			if err := protojson.Unmarshal([]byte(stdout), page); err != nil {
				t.Fatal(err)
			}
			if len(page.GetSessions()) != 1 || page.GetSessions()[0].GetStatus().GetMaterialization() != dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING || page.GetNextPageToken() != "next-page" || stderr != "" {
				t.Fatalf("list JSON page: %v, stderr %q", page, stderr)
			}
		} else if !strings.Contains(stdout, "58cae82c-453d-4de7-ae5b-17a8f7596077") || !strings.Contains(stdout, "waiting") || !strings.Contains(stderr, "--after next-page") {
			t.Fatalf("list text: stdout %q, stderr %q", stdout, stderr)
		}
	}
}

type sessionListFixture struct {
	dispatcherpb.UnimplementedSessionDispatcherServer
	requests       chan *dispatcherpb.ListSessionsRequest
	authorizations chan []string
}

func (f *sessionListFixture) ListSessions(ctx context.Context, request *dispatcherpb.ListSessionsRequest) (*dispatcherpb.ListSessionsResponse, error) {
	f.requests <- request
	authorization, _ := metadata.FromIncomingContext(ctx)
	f.authorizations <- authorization.Get("authorization")
	return &dispatcherpb.ListSessionsResponse{
		Sessions: []*dispatcherpb.Session{{
			SessionId: "58cae82c-453d-4de7-ae5b-17a8f7596077", Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: &sessionpb.ExactEnvironment{Name: "queued-sandbox", Uid: "queued-sandbox-uid"}}}},
			Status: &dispatcherpb.SessionStatus{Materialization: dispatcherpb.SessionStatus_SESSION_MATERIALIZATION_WAITING},
		}},
		NextPageToken: "next-page",
	}, nil
}
