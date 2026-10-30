package main

import (
	"context"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	bbspb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	invocationstatuspb "github.com/buildbuddy-io/buildbuddy/proto/invocation_status"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

type invocationListTestServer struct {
	bbspb.UnimplementedBuildBuddyServiceServer
	mu       sync.Mutex
	requests []*invocationpb.SearchInvocationRequest
	pages    map[string]*invocationpb.SearchInvocationResponse
}

func (s *invocationListTestServer) SearchInvocation(_ context.Context, req *invocationpb.SearchInvocationRequest) (*invocationpb.SearchInvocationResponse, error) {
	s.mu.Lock()
	defer s.mu.Unlock()
	s.requests = append(s.requests, proto.Clone(req).(*invocationpb.SearchInvocationRequest))
	return proto.Clone(s.pages[req.GetPageToken()]).(*invocationpb.SearchInvocationResponse), nil
}

func startInvocationListTestServer(t *testing.T, service *invocationListTestServer) {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatalf("listen for gRPC test server: %v", err)
	}
	server := grpc.NewServer()
	bbspb.RegisterBuildBuddyServiceServer(server, service)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	t.Setenv("BUILDBUDDY_API_KEY", "test-key")
	t.Setenv("BUILDBUDDY_URL", "http://"+listener.Addr().String())
}

func (s *invocationListTestServer) capturedRequests() []*invocationpb.SearchInvocationRequest {
	s.mu.Lock()
	defer s.mu.Unlock()
	return append([]*invocationpb.SearchInvocationRequest(nil), s.requests...)
}

func TestInvocationListFiltersAndPaginatesAllPages(t *testing.T) {
	service := &invocationListTestServer{pages: map[string]*invocationpb.SearchInvocationResponse{
		"": {
			Invocation:    []*invocationpb.Invocation{{InvocationId: "invocation-1"}},
			NextPageToken: "page-2",
		},
		"page-2": {
			Invocation: []*invocationpb.Invocation{{InvocationId: "invocation-2"}},
		},
	}}
	startInvocationListTestServer(t, service)

	previousJSONOutput := jsonOutput
	jsonOutput = true
	t.Cleanup(func() { jsonOutput = previousJSONOutput })

	cmd := invocationListCmd()
	flags := map[string]string{
		"group":          "test-group",
		"repo":           "https://repo.test/project",
		"count":          "2",
		"updated-after":  "2026-10-29T00:00:00Z",
		"updated-before": "2026-10-30T00:00:00Z",
		"all":            "true",
	}
	for name, value := range flags {
		if err := cmd.Flags().Set(name, value); err != nil {
			t.Fatalf("set --%s: %v", name, err)
		}
	}
	if err := cmd.Flags().Set("status", "in-progress,success"); err != nil {
		t.Fatalf("set comma-separated --status: %v", err)
	}
	if err := cmd.Flags().Set("status", "failure"); err != nil {
		t.Fatalf("repeat --status: %v", err)
	}

	output, err := captureStdout(t, func() error { return cmd.RunE(cmd, nil) })
	if err != nil {
		t.Fatalf("run invocation list: %v", err)
	}
	response := &invocationpb.SearchInvocationResponse{}
	if err := protojson.Unmarshal(output, response); err != nil {
		t.Fatalf("decode aggregated JSON output: %v\n%s", err, output)
	}
	if got := len(response.GetInvocation()); got != 2 {
		t.Fatalf("JSON invocation count = %d, want 2", got)
	}
	if got := response.GetNextPageToken(); got != "" {
		t.Fatalf("JSON next page token = %q, want empty after --all", got)
	}
	if got := response.GetInvocation()[1].GetInvocationId(); got != "invocation-2" {
		t.Errorf("second JSON invocation = %q, want invocation-2", got)
	}

	requests := service.capturedRequests()
	if len(requests) != 2 {
		t.Fatalf("SearchInvocation request count = %d, want 2", len(requests))
	}
	first, second := requests[0], requests[1]
	for i, req := range requests {
		if got := req.GetQuery().GetGroupId(); got != "test-group" {
			t.Errorf("request %d query group id = %q, want test-group", i, got)
		}
		if got := req.GetRequestContext().GetGroupId(); got != "test-group" {
			t.Errorf("request %d context group id = %q, want test-group", i, got)
		}
		if got := req.GetQuery().GetRepoUrl(); got != "https://repo.test/project" {
			t.Errorf("request %d repo = %q", i, got)
		}
		if got := req.GetCount(); got != 2 {
			t.Errorf("request %d count = %d, want page size 2", i, got)
		}
		if got := req.GetQuery().GetStatus(); len(got) != 3 || got[0] != invocationstatuspb.OverallStatus_IN_PROGRESS || got[1] != invocationstatuspb.OverallStatus_SUCCESS || got[2] != invocationstatuspb.OverallStatus_FAILURE {
			t.Errorf("request %d statuses = %v", i, got)
		}
		if got := req.GetQuery().GetUpdatedAfter().AsTime(); !got.Equal(mustParseInvocationTestTime(t, "2026-10-29T00:00:00Z")) {
			t.Errorf("request %d updated after = %s", i, got)
		}
		if got := req.GetQuery().GetUpdatedBefore().AsTime(); !got.Equal(mustParseInvocationTestTime(t, "2026-10-30T00:00:00Z")) {
			t.Errorf("request %d updated before = %s", i, got)
		}
	}
	if first.GetPageToken() != "" || second.GetPageToken() != "page-2" {
		t.Errorf("page tokens = %q, %q; want empty, page-2", first.GetPageToken(), second.GetPageToken())
	}
}

func TestInvocationListKeepsDefaultRepoQueryAndSinglePage(t *testing.T) {
	service := &invocationListTestServer{pages: map[string]*invocationpb.SearchInvocationResponse{
		"": {
			Invocation:    []*invocationpb.Invocation{{InvocationId: "first-page"}},
			NextPageToken: "page-2",
		},
	}}
	startInvocationListTestServer(t, service)
	previousJSONOutput := jsonOutput
	jsonOutput = true
	t.Cleanup(func() { jsonOutput = previousJSONOutput })

	cmd := invocationListCmd()
	if err := cmd.Flags().Set("repo", "https://repo.test/project"); err != nil {
		t.Fatal(err)
	}
	output, err := captureStdout(t, func() error { return cmd.RunE(cmd, nil) })
	if err != nil {
		t.Fatalf("run invocation list: %v", err)
	}
	response := &invocationpb.SearchInvocationResponse{}
	if err := protojson.Unmarshal(output, response); err != nil {
		t.Fatalf("decode JSON output: %v\n%s", err, output)
	}
	if got := response.GetNextPageToken(); got != "page-2" {
		t.Errorf("single-page JSON next page token = %q, want page-2", got)
	}
	requests := service.capturedRequests()
	if len(requests) != 1 {
		t.Fatalf("SearchInvocation request count = %d, want 1", len(requests))
	}
	if got := requests[0].GetQuery().GetRepoUrl(); got != "https://repo.test/project" {
		t.Errorf("query repo = %q", got)
	}
	if got := requests[0].GetQuery().GetGroupId(); got != "" {
		t.Errorf("default query group id = %q, want empty", got)
	}
	if got := requests[0].GetRequestContext(); got != nil {
		t.Errorf("default request context = %v, want nil", got)
	}
	if got := requests[0].GetCount(); got != 10 {
		t.Errorf("default page size = %d, want 10", got)
	}
}

func TestInvocationListGroupWithoutRepoDoesNotAutodetectRepository(t *testing.T) {
	service := &invocationListTestServer{pages: map[string]*invocationpb.SearchInvocationResponse{
		"": {Invocation: []*invocationpb.Invocation{{InvocationId: "group-invocation"}}},
	}}
	startInvocationListTestServer(t, service)
	previousJSONOutput := jsonOutput
	jsonOutput = false
	t.Cleanup(func() { jsonOutput = previousJSONOutput })

	cmd := invocationListCmd()
	if err := cmd.Flags().Set("group", "test-group"); err != nil {
		t.Fatal(err)
	}
	output, err := captureStdout(t, func() error { return cmd.RunE(cmd, nil) })
	if err != nil {
		t.Fatalf("run invocation list: %v", err)
	}
	if !strings.Contains(string(output), "group-invocation") {
		t.Fatalf("table output does not contain invocation: %s", output)
	}
	requests := service.capturedRequests()
	if len(requests) != 1 {
		t.Fatalf("SearchInvocation request count = %d, want 1", len(requests))
	}
	if got := requests[0].GetQuery().GetRepoUrl(); got != "" {
		t.Errorf("group-only query repo = %q, want empty", got)
	}
	if got := requests[0].GetQuery().GetGroupId(); got != "test-group" {
		t.Errorf("query group id = %q", got)
	}
	if got := requests[0].GetRequestContext().GetGroupId(); got != "test-group" {
		t.Errorf("request context group id = %q", got)
	}
}

func TestInvocationListAllRejectsRepeatedPageToken(t *testing.T) {
	service := &invocationListTestServer{pages: map[string]*invocationpb.SearchInvocationResponse{
		"":       {NextPageToken: "repeat"},
		"repeat": {NextPageToken: "repeat"},
	}}
	startInvocationListTestServer(t, service)
	cmd := invocationListCmd()
	if err := cmd.Flags().Set("group", "test-group"); err != nil {
		t.Fatal(err)
	}
	if err := cmd.Flags().Set("all", "true"); err != nil {
		t.Fatal(err)
	}
	if _, err := captureStdout(t, func() error { return cmd.RunE(cmd, nil) }); err == nil || !strings.Contains(err.Error(), "repeated page token") {
		t.Fatalf("run invocation list error = %v, want repeated page token", err)
	}
	if got := len(service.capturedRequests()); got != 2 {
		t.Errorf("SearchInvocation request count = %d, want 2", got)
	}
}

func TestInvocationListRejectsInvalidFiltersBeforeCreatingClient(t *testing.T) {
	cases := []struct {
		name  string
		flags map[string]string
	}{
		{name: "status", flags: map[string]string{"status": "running-ish"}},
		{name: "timestamp", flags: map[string]string{"updated-after": "yesterday"}},
		{name: "timestamp order", flags: map[string]string{
			"updated-after":  "2026-10-30T00:00:00Z",
			"updated-before": "2026-10-29T00:00:00Z",
		}},
	}
	t.Setenv("BUILDBUDDY_API_KEY", "")
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			cmd := invocationListCmd()
			for name, value := range tc.flags {
				if err := cmd.Flags().Set(name, value); err != nil {
					t.Fatalf("set --%s: %v", name, err)
				}
			}
			err := cmd.RunE(cmd, nil)
			if err == nil {
				t.Fatal("RunE succeeded for invalid filters")
			}
			if strings.Contains(err.Error(), "BUILDBUDDY_API_KEY") {
				t.Fatalf("invalid filters reached client creation: %v", err)
			}
		})
	}
}

func mustParseInvocationTestTime(t *testing.T, value string) time.Time {
	t.Helper()
	timestamp, err := time.Parse(time.RFC3339, value)
	if err != nil {
		t.Fatal(err)
	}
	return timestamp
}
