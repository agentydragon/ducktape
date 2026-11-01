package main

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/history_servicepb"
	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc"
)

func TestObserveInputRequiresItsOwnHistoryConfiguration(t *testing.T) {
	env := map[string]string{
		"AGENTPLANE_SESSIONS_URL":        "http://127.0.0.1:1",
		"AGENTPLANE_SESSIONS_CREDENTIAL": "dispatcher-placeholder",
	}
	code, stdout, stderr := invoke(env, "sessions", "observe-input", "58cae82c-453d-4de7-ae5b-17a8f7596077", "--command-id", "c40dd833-326f-4be1-b42a-4bb74c76c49a")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "AGENTPLANE_HISTORY_URL and AGENTPLANE_HISTORY_CREDENTIAL not set") {
		t.Fatalf("exit %d, stdout %q, stderr %q; want independent History configuration error", code, stdout, stderr)
	}
}

func TestObserveInputRejectsWaitOverBoundBeforeConnecting(t *testing.T) {
	code, stdout, stderr := invoke(nil, "sessions", "observe-input", "session", "--command-id", "command", "--wait-seconds", "31")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "--wait-seconds must be between 0 and 30") {
		t.Fatalf("exit %d, stdout %q, stderr %q; want bounded wait validation", code, stdout, stderr)
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
	opening := snapshot["opening"].(map[string]any)
	history := snapshot["history"].(map[string]any)
	if opening["kind"] != "observed" || history["kind"] != "observed" {
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

func (sessionProjectionFixture) GetSessionOpening(_ context.Context, request *dispatcherpb.GetSessionOpeningRequest) (*dispatcherpb.GetSessionOpeningResponse, error) {
	return &dispatcherpb.GetSessionOpeningResponse{Opening: &dispatcherpb.SessionOpening{
		SessionId: request.GetSessionId(), State: dispatcherpb.DeliveryState_DELIVERY_STATE_RUNNER_ADMITTED,
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
