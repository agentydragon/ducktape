package main

import (
	"context"
	"encoding/json"
	"io"
	"net"
	"os"
	"strings"
	"testing"
	"time"

	aclpb "github.com/buildbuddy-io/buildbuddy/proto/acl"
	bespb "github.com/buildbuddy-io/buildbuddy/proto/build_event_stream"
	bbspb "github.com/buildbuddy-io/buildbuddy/proto/buildbuddy_service"
	invocationpb "github.com/buildbuddy-io/buildbuddy/proto/invocation"
	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
	"google.golang.org/protobuf/proto"
)

type targetTestServer struct {
	bbspb.UnimplementedBuildBuddyServiceServer
	t             *testing.T
	searchResult  *invocationpb.SearchInvocationResponse
	pageTokens    []string
	pages         map[string]*targetpb.GetTargetHistoryResponse
	statsRequests []*targetpb.GetTargetStatsRequest
	flakeRequests []*targetpb.GetTargetFlakeSamplesRequest
	flakePages    map[string]*targetpb.GetTargetFlakeSamplesResponse
}

func (s *targetTestServer) SearchInvocation(context.Context, *invocationpb.SearchInvocationRequest) (*invocationpb.SearchInvocationResponse, error) {
	return s.searchResult, nil
}

func (s *targetTestServer) GetTargetHistory(_ context.Context, req *targetpb.GetTargetHistoryRequest) (*targetpb.GetTargetHistoryResponse, error) {
	pageToken := req.GetPageToken()
	s.pageTokens = append(s.pageTokens, pageToken)
	page, ok := s.pages[pageToken]
	if !ok {
		s.t.Errorf("unexpected page token %q", pageToken)
		return nil, grpcstatus.Error(codes.InvalidArgument, "unexpected page token")
	}
	return page, nil
}

func (s *targetTestServer) GetTargetStats(_ context.Context, req *targetpb.GetTargetStatsRequest) (*targetpb.GetTargetStatsResponse, error) {
	s.statsRequests = append(s.statsRequests, proto.Clone(req).(*targetpb.GetTargetStatsRequest))
	return &targetpb.GetTargetStatsResponse{}, nil
}

func (s *targetTestServer) GetTargetFlakeSamples(_ context.Context, req *targetpb.GetTargetFlakeSamplesRequest) (*targetpb.GetTargetFlakeSamplesResponse, error) {
	s.flakeRequests = append(s.flakeRequests, proto.Clone(req).(*targetpb.GetTargetFlakeSamplesRequest))
	page, ok := s.flakePages[req.GetPageToken()]
	if !ok {
		s.t.Errorf("unexpected flake page token %q", req.GetPageToken())
		return nil, grpcstatus.Error(codes.InvalidArgument, "unexpected page token")
	}
	return page, nil
}

func startTargetTestServer(t *testing.T, service *targetTestServer) {
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

func decodeTargetHistoryPage(t *testing.T, data []byte) *targetpb.GetTargetHistoryResponse {
	t.Helper()
	response := &targetpb.GetTargetHistoryResponse{}
	if err := protojson.Unmarshal(data, response); err != nil {
		t.Fatalf("decode target history fixture: %v", err)
	}
	return response
}

func decodeTargetFlakePage(t *testing.T, data []byte) *targetpb.GetTargetFlakeSamplesResponse {
	t.Helper()
	response := &targetpb.GetTargetFlakeSamplesResponse{}
	if err := protojson.Unmarshal(data, response); err != nil {
		t.Fatalf("decode target flake fixture: %v", err)
	}
	return response
}

func TestChooseLogsPrefersTheResultsThatFailed(t *testing.T) {
	logs := logsOf(parseOrFail(t, shardedStream), "//pkg:visual")
	chosen, why := chooseLogs(logs)
	if len(chosen) != 1 || chosen[0].GetId().GetTestResult().GetShard() != 2 {
		t.Errorf("chose %v", describeAll(chosen))
	}
	if !strings.Contains(why, "1 of 3") {
		t.Errorf("why = %q", why)
	}
}

func TestChooseLogsFallsBackToTheFirstWhenNothingFailed(t *testing.T) {
	logs := logsOf(parseOrFail(t, shardedStream), "//pkg:visual")
	logs[1].Status = bespb.TestStatus_PASSED
	chosen, why := chooseLogs(logs)
	if len(chosen) != 1 || chosen[0].GetId().GetTestResult().GetShard() != 1 {
		t.Errorf("chose %v", describeAll(chosen))
	}
	if !strings.Contains(why, "None failed") {
		t.Errorf("why = %q", why)
	}
}

func TestTargetHistoryPageCutoffRequiresValidTimestamps(t *testing.T) {
	page := &targetpb.GetTargetHistoryResponse{
		InvocationTargets: []*targetpb.TargetHistory{{
			TargetStatus: []*targetpb.TargetStatus{{}},
		}},
	}
	if targetHistoryPageIsOlderThan(page, time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)) {
		t.Fatal("page with a missing invocation timestamp was treated as an old page")
	}
}

func TestTargetHistoryPaginatesMergesAndFiltersBothOutputModes(t *testing.T) {
	cases := []struct {
		name       string
		jsonOutput bool
		flags      map[string]string
	}{
		{
			name: "text filters before count",
			flags: map[string]string{
				"failures-only": "true",
				"since":         "2026-01-01",
				"count":         "1",
			},
		},
		{
			name:       "json filters merged pages",
			jsonOutput: true,
			flags: map[string]string{
				"label":         "//pkg:keep",
				"failures-only": "true",
				"since":         "2026-01-01",
			},
		},
		{
			name:       "json applies count",
			jsonOutput: true,
			flags: map[string]string{
				"failures-only": "true",
				"since":         "2026-01-01",
				"count":         "1",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			firstPage := []byte(`{"invocationTargets":[
				{"target":{"label":"//pkg:passed"},"targetStatus":[{"status":"PASSED","invocationId":"passed-only","invocationCreatedAtUsec":"1772323200000000"}]},
				{"target":{"label":"//pkg:keep"},"targetStatus":[{"status":"PASSED","invocationId":"keep-pass","invocationCreatedAtUsec":"1772409600000000"}]}
			],"nextPageToken":"page-2"}`)
			secondPage := []byte(`{"invocationTargets":[
				{"target":{"label":"//pkg:keep"},"targetStatus":[{"status":"FAILED","invocationId":"keep-cutoff","invocationCreatedAtUsec":"1767225600000000"},{"status":"FAILED","invocationId":"old-failure","invocationCreatedAtUsec":"1767139200000000"}]},
				{"target":{"label":"//pkg:other"},"targetStatus":[{"status":"FAILED","invocationId":"other-failure","invocationCreatedAtUsec":"1767225600000000"}]}
			],"nextPageToken":"page-3"}`)
			thirdPage := []byte(`{"invocationTargets":[
				{"target":{"label":"//pkg:keep"},"targetStatus":[{"status":"FAILED","invocationId":"stale-page","invocationCreatedAtUsec":"1767052800000000"}]}
			],"nextPageToken":"page-4"}`)
			service := &targetTestServer{
				t: t,
				searchResult: &invocationpb.SearchInvocationResponse{
					Invocation: []*invocationpb.Invocation{{Acl: &aclpb.ACL{GroupId: "test-group"}}},
				},
				pages: map[string]*targetpb.GetTargetHistoryResponse{
					"":       decodeTargetHistoryPage(t, firstPage),
					"page-2": decodeTargetHistoryPage(t, secondPage),
					"page-3": decodeTargetHistoryPage(t, thirdPage),
				},
			}
			startTargetTestServer(t, service)

			previousJSONOutput := jsonOutput
			jsonOutput = tc.jsonOutput
			t.Cleanup(func() { jsonOutput = previousJSONOutput })

			cmd := targetHistorySubCmd()
			if err := cmd.Flags().Set("repo", "https://repo.test/project"); err != nil {
				t.Fatal(err)
			}
			for name, value := range tc.flags {
				if err := cmd.Flags().Set(name, value); err != nil {
					t.Fatal(err)
				}
			}
			output, err := captureTargetCommandStdout(t, func() error { return cmd.RunE(cmd, nil) })
			if err != nil {
				t.Fatalf("run target history: %v", err)
			}
			if len(service.pageTokens) != 3 || service.pageTokens[0] != "" || service.pageTokens[1] != "page-2" || service.pageTokens[2] != "page-3" {
				t.Errorf("history page tokens = %q, want empty, page-2, page-3; page-4 must be skipped", service.pageTokens)
			}

			if tc.jsonOutput {
				response := &targetpb.GetTargetHistoryResponse{}
				if err := protojson.Unmarshal(output, response); err != nil {
					t.Fatalf("decode JSON output: %v\n%s", err, output)
				}
				if got := len(response.GetInvocationTargets()); got != 1 {
					t.Fatalf("JSON target count = %d, want 1", got)
				}
				history := response.GetInvocationTargets()[0]
				if got := history.GetTarget().GetLabel(); got != "//pkg:keep" {
					t.Errorf("JSON target label = %q", got)
				}
				if got := len(history.GetTargetStatus()); got != 1 || history.GetTargetStatus()[0].GetInvocationId() != "keep-cutoff" {
					t.Errorf("JSON statuses = %v, want only keep-cutoff", history.GetTargetStatus())
				}
				var fields map[string]json.RawMessage
				if err := json.Unmarshal(output, &fields); err != nil {
					t.Fatalf("decode JSON fields: %v", err)
				}
				if _, ok := fields["nextPageToken"]; ok {
					t.Errorf("JSON contains a stale nextPageToken: %s", output)
				}
				return
			}

			if got := strings.Count(string(output), "Target: //pkg:keep"); got != 1 {
				t.Errorf("text target count = %d, want one merged target:\n%s", got, output)
			}
			for _, excluded := range []string{"passed-only", "keep-pass", "old-failure", "other-failure", "stale-page"} {
				if strings.Contains(string(output), excluded) {
					t.Errorf("text output contains filtered invocation %q:\n%s", excluded, output)
				}
			}
			if !strings.Contains(string(output), "keep-cutoff") {
				t.Errorf("text output omits the inclusive cutoff history:\n%s", output)
			}
		})
	}
}

func TestTargetStatsPassesFiltersAndPreservesDefaultWindow(t *testing.T) {
	cases := []struct {
		name       string
		flags      map[string]string
		wantBranch string
		wantAfter  time.Time
		wantBefore time.Time
	}{
		{
			name: "filters by branch and date bounds",
			flags: map[string]string{
				"branch": "devel",
				"since":  "2026-10-01",
				"until":  "2026-10-07",
			},
			wantBranch: "devel",
			wantAfter:  time.Date(2026, 10, 1, 0, 0, 0, 0, time.UTC),
			wantBefore: time.Date(2026, 10, 7, 0, 0, 0, 0, time.UTC),
		},
		{name: "leaves default window to server"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			service := &targetTestServer{
				t: t,
				searchResult: &invocationpb.SearchInvocationResponse{
					Invocation: []*invocationpb.Invocation{{Acl: &aclpb.ACL{GroupId: "test-group"}}},
				},
			}
			startTargetTestServer(t, service)

			previousJSONOutput := jsonOutput
			jsonOutput = true
			t.Cleanup(func() { jsonOutput = previousJSONOutput })

			cmd := targetStatsSubCmd()
			if err := cmd.Flags().Set("repo", "https://repo.test/project"); err != nil {
				t.Fatal(err)
			}
			for name, value := range tc.flags {
				if err := cmd.Flags().Set(name, value); err != nil {
					t.Fatal(err)
				}
			}
			if _, err := captureTargetCommandStdout(t, func() error { return cmd.RunE(cmd, nil) }); err != nil {
				t.Fatalf("run target stats: %v", err)
			}
			if len(service.statsRequests) != 1 {
				t.Fatalf("stats request count = %d, want 1", len(service.statsRequests))
			}
			request := service.statsRequests[0]
			if request.GetRequestContext().GetGroupId() != "test-group" || request.GetRepo() != "https://repo.test/project" {
				t.Errorf("stats request context/repo = %q/%q", request.GetRequestContext().GetGroupId(), request.GetRepo())
			}
			if request.GetBranchName() != tc.wantBranch {
				t.Errorf("stats branch = %q, want %q", request.GetBranchName(), tc.wantBranch)
			}
			if tc.wantAfter.IsZero() {
				if request.GetStartedAfter() != nil || request.GetStartedBefore() != nil {
					t.Errorf("unset time filters were sent: after=%v before=%v", request.GetStartedAfter(), request.GetStartedBefore())
				}
				return
			}
			if request.GetStartedAfter() == nil || request.GetStartedBefore() == nil {
				t.Fatalf("explicit time filters were omitted: after=%v before=%v", request.GetStartedAfter(), request.GetStartedBefore())
			}
			if got := request.GetStartedAfter().AsTime(); !got.Equal(tc.wantAfter) {
				t.Errorf("started_after = %s, want %s", got, tc.wantAfter)
			}
			if got := request.GetStartedBefore().AsTime(); !got.Equal(tc.wantBefore) {
				t.Errorf("started_before = %s, want %s", got, tc.wantBefore)
			}
		})
	}
}

func TestTargetTimeRangeRejectsReversedBounds(t *testing.T) {
	now := time.Date(2026, 10, 30, 12, 0, 0, 0, time.UTC)
	if _, _, err := parseTargetTimeRange("2026-10-08", "2026-10-07", now); err == nil {
		t.Fatal("reversed target time range was accepted")
	}
}

func TestTargetFlakesPaginatesAndReturnsCombinedJSON(t *testing.T) {
	service := &targetTestServer{
		t: t,
		searchResult: &invocationpb.SearchInvocationResponse{
			Invocation: []*invocationpb.Invocation{{Acl: &aclpb.ACL{GroupId: "test-group"}}},
		},
		flakePages: map[string]*targetpb.GetTargetFlakeSamplesResponse{
			"":       decodeTargetFlakePage(t, []byte(`{"samples":[{"invocationId":"sample-1"},{"invocationId":"sample-2"}],"nextPageToken":"page-2"}`)),
			"page-2": decodeTargetFlakePage(t, []byte(`{"samples":[{"invocationId":"sample-3"}]}`)),
		},
	}
	startTargetTestServer(t, service)

	previousJSONOutput := jsonOutput
	jsonOutput = true
	t.Cleanup(func() { jsonOutput = previousJSONOutput })

	cmd := targetFlakesSubCmd()
	for name, value := range map[string]string{
		"repo":   "https://repo.test/project",
		"branch": "devel",
		"since":  "24h",
		"until":  "12h",
	} {
		if err := cmd.Flags().Set(name, value); err != nil {
			t.Fatal(err)
		}
	}
	runStarted := time.Now()
	output, err := captureTargetCommandStdout(t, func() error { return cmd.RunE(cmd, []string{"//pkg:flaky"}) })
	runFinished := time.Now()
	if err != nil {
		t.Fatalf("run target flakes: %v", err)
	}
	if len(service.flakeRequests) != 2 {
		t.Fatalf("flake request count = %d, want 2", len(service.flakeRequests))
	}
	if got := []string{service.flakeRequests[0].GetPageToken(), service.flakeRequests[1].GetPageToken()}; got[0] != "" || got[1] != "page-2" {
		t.Errorf("page tokens = %q, want empty then page-2", got)
	}
	for _, request := range service.flakeRequests {
		if request.GetRequestContext().GetGroupId() != "test-group" || request.GetRepo() != "https://repo.test/project" || request.GetLabel() != "//pkg:flaky" || request.GetBranchName() != "devel" {
			t.Errorf("flake request filters = %#v", request)
		}
		if request.GetStartedAfter() == nil || request.GetStartedBefore() == nil {
			t.Errorf("flake request time range = %v..%v, want 12h", request.GetStartedAfter(), request.GetStartedBefore())
			continue
		}
		startedAfter := request.GetStartedAfter().AsTime()
		startedBefore := request.GetStartedBefore().AsTime()
		if startedBefore.Sub(startedAfter) != 12*time.Hour {
			t.Errorf("flake request time range = %v..%v, want 12h", startedAfter, startedBefore)
		}
		if startedAfter.Before(runStarted.Add(-24*time.Hour)) || startedAfter.After(runFinished.Add(-24*time.Hour)) {
			t.Errorf("started_after = %s, want time.Now() - 24h", startedAfter)
		}
		if startedBefore.Before(runStarted.Add(-12*time.Hour)) || startedBefore.After(runFinished.Add(-12*time.Hour)) {
			t.Errorf("started_before = %s, want time.Now() - 12h", startedBefore)
		}
	}
	response := &targetpb.GetTargetFlakeSamplesResponse{}
	if err := protojson.Unmarshal(output, response); err != nil {
		t.Fatalf("decode combined flake JSON: %v\n%s", err, output)
	}
	if got := len(response.GetSamples()); got != 3 {
		t.Fatalf("combined sample count = %d, want 3", got)
	}
	for i, want := range []string{"sample-1", "sample-2", "sample-3"} {
		if got := response.GetSamples()[i].GetInvocationId(); got != want {
			t.Errorf("sample %d invocation = %q, want %q", i, got, want)
		}
	}
	if response.GetNextPageToken() != "" {
		t.Errorf("combined response kept next page token %q", response.GetNextPageToken())
	}
}

func TestTargetFlakesRejectsRepeatedPageTokenBeforeOutput(t *testing.T) {
	service := &targetTestServer{
		t: t,
		searchResult: &invocationpb.SearchInvocationResponse{
			Invocation: []*invocationpb.Invocation{{Acl: &aclpb.ACL{GroupId: "test-group"}}},
		},
		flakePages: map[string]*targetpb.GetTargetFlakeSamplesResponse{
			"":       decodeTargetFlakePage(t, []byte(`{"samples":[{"invocationId":"sample-1"}],"nextPageToken":"page-2"}`)),
			"page-2": decodeTargetFlakePage(t, []byte(`{"samples":[{"invocationId":"sample-2"}],"nextPageToken":"page-2"}`)),
		},
	}
	startTargetTestServer(t, service)

	previousJSONOutput := jsonOutput
	jsonOutput = true
	t.Cleanup(func() { jsonOutput = previousJSONOutput })

	cmd := targetFlakesSubCmd()
	if err := cmd.Flags().Set("repo", "https://repo.test/project"); err != nil {
		t.Fatal(err)
	}
	output, err := captureTargetCommandStdout(t, func() error { return cmd.RunE(cmd, []string{"//pkg:flaky"}) })
	if err == nil {
		t.Fatal("run target flakes with a repeated page token succeeded")
	}
	if len(service.flakeRequests) != 2 {
		t.Errorf("flake request count = %d, want 2", len(service.flakeRequests))
	}
	if len(output) != 0 {
		t.Errorf("partial JSON was printed before pagination completed: %s", output)
	}
}

func captureTargetCommandStdout(t *testing.T, run func() error) ([]byte, error) {
	t.Helper()
	reader, writer, err := os.Pipe()
	if err != nil {
		return nil, err
	}
	previousStdout := os.Stdout
	os.Stdout = writer
	defer func() {
		os.Stdout = previousStdout
		_ = reader.Close()
	}()
	runErr := run()
	if err := writer.Close(); err != nil {
		return nil, err
	}
	os.Stdout = previousStdout
	output, err := io.ReadAll(reader)
	if err != nil {
		return nil, err
	}
	return output, runErr
}
