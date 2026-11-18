package cli

import (
	"context"
	"strings"
	"testing"

	historypb "github.com/agentydragon/ducktape/agentplane/history_service/api/history/history_servicepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

type historyFixture struct {
	historypb.UnimplementedHistoryServiceServer
	requests      chan *historypb.ReadSessionEventsRequest
	authorization chan []string
}

func (f *historyFixture) GetSessionSummary(_ context.Context, request *historypb.GetSessionSummaryRequest) (*historypb.SessionSummary, error) {
	if request.GetSessionId() == "foreign" {
		return nil, status.Error(codes.NotFound, "not found")
	}
	return &historypb.SessionSummary{SessionId: request.GetSessionId()}, nil
}

func (f *historyFixture) ReadSessionEvents(ctx context.Context, request *historypb.ReadSessionEventsRequest) (*historypb.ReadSessionEventsResponse, error) {
	f.requests <- request
	metadata, _ := metadata.FromIncomingContext(ctx)
	f.authorization <- metadata.Get("authorization")
	return &historypb.ReadSessionEventsResponse{LastCursor: 17, Removed: []*historypb.CursorRange{{First: 7, Last: 9}}}, nil
}

func TestHistoryCommandsCallNativeRPCs(t *testing.T) {
	server := grpc.NewServer()
	fixture := &historyFixture{requests: make(chan *historypb.ReadSessionEventsRequest, 1), authorization: make(chan []string, 1)}
	historypb.RegisterHistoryServiceServer(server, fixture)
	address := grpcserver.Serve(t, server)
	env := map[string]string{"AGENTPLANE_HISTORY_URL": "http://" + address}
	code, stdout, stderr := invoke(env, "--json", "history", "get-session-summary", "owned")
	summary := &historypb.SessionSummary{}
	if code != 0 || stderr != "" || protojson.Unmarshal([]byte(stdout), summary) != nil || summary.GetSessionId() != "owned" {
		t.Fatalf("summary: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
	code, stdout, stderr = invoke(env, "--json", "history", "read-session-events", "owned", "--after", "5", "--limit", "10")
	response := &historypb.ReadSessionEventsResponse{}
	if code != 0 || stderr != "" || protojson.Unmarshal([]byte(stdout), response) != nil || response.GetLastCursor() != 17 || len(response.GetRemoved()) != 1 {
		t.Fatalf("events: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
	request := <-fixture.requests
	if request.GetSessionId() != "owned" || request.GetAfterCursor() != 5 || request.GetLimit() != 10 {
		t.Fatalf("request = %v", request)
	}
	if authorization := <-fixture.authorization; len(authorization) != 0 {
		t.Fatalf("unexpected bearer %v", authorization)
	}
	code, stdout, stderr = invoke(env, "--json", "history", "get-session-summary", "foreign")
	if code != 5 || stdout != "" || !strings.Contains(stderr, "NOT_FOUND") {
		t.Fatalf("foreign summary: exit %d, stdout %q, stderr %q", code, stdout, stderr)
	}
}
