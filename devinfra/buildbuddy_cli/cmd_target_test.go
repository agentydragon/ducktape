package main

import (
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"

	targetpb "github.com/buildbuddy-io/buildbuddy/proto/target"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestChooseLogsPrefersTheResultsThatFailed(t *testing.T) {
	logs := logsOf(parseOrFail(t, shardedStream), "//pkg:visual")
	chosen, why := chooseLogs(logs)
	if len(chosen) != 1 || chosen[0].Shard != 2 {
		t.Errorf("chose %v", describeAll(chosen))
	}
	if !strings.Contains(why, "1 of 3") {
		t.Errorf("why = %q", why)
	}
}

func TestChooseLogsFallsBackToTheFirstWhenNothingFailed(t *testing.T) {
	logs := logsOf(parseOrFail(t, shardedStream), "//pkg:visual")
	logs[1].Status = "PASSED"
	chosen, why := chooseLogs(logs)
	if len(chosen) != 1 || chosen[0].Shard != 1 {
		t.Errorf("chose %v", describeAll(chosen))
	}
	if !strings.Contains(why, "None failed") {
		t.Errorf("why = %q", why)
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
				"count":         "1",
			},
		},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			var pageTokens []string
			firstPage := []byte(`{"invocationTargets":[
				{"target":{"label":"//pkg:passed"},"targetStatus":[{"status":"PASSED","invocationId":"passed-only","invocationCreatedAtUsec":"1772323200000000"}]},
				{"target":{"label":"//pkg:keep"},"targetStatus":[{"status":"PASSED","invocationId":"keep-pass","invocationCreatedAtUsec":"1772409600000000"},{"status":"FAILED","invocationId":"old-failure","invocationCreatedAtUsec":"1767139200000000"}]}
			],"nextPageToken":"page-2"}`)
			secondPage := []byte(`{"invocationTargets":[
				{"target":{"label":"//pkg:keep"},"targetStatus":[{"status":"FAILED","invocationId":"keep-failure","invocationCreatedAtUsec":"1777593600000000"}]},
				{"target":{"label":"//pkg:other"},"targetStatus":[{"status":"FAILED","invocationId":"other-failure","invocationCreatedAtUsec":"1777680000000000"}]}
			]}`)
			server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
				w.Header().Set("Content-Type", "application/json")
				switch r.URL.Path {
				case "/rpc/BuildBuddyService/SearchInvocation":
					_, _ = io.WriteString(w, `{"invocation":[{"acl":{"groupId":"test-group"}}]}`)
				case "/rpc/BuildBuddyService/GetTargetHistory":
					var request struct {
						PageToken string `json:"pageToken"`
					}
					if err := json.NewDecoder(r.Body).Decode(&request); err != nil {
						t.Errorf("decode history request: %v", err)
						http.Error(w, "bad request", http.StatusBadRequest)
						return
					}
					pageTokens = append(pageTokens, request.PageToken)
					if request.PageToken == "" {
						_, _ = w.Write(firstPage)
						return
					}
					if request.PageToken != "page-2" {
						t.Errorf("page token = %q, want empty or page-2", request.PageToken)
						http.Error(w, "unexpected page token", http.StatusBadRequest)
						return
					}
					_, _ = w.Write(secondPage)
				default:
					t.Errorf("unexpected API path %q", r.URL.Path)
					http.NotFound(w, r)
				}
			}))
			defer server.Close()
			t.Setenv("BUILDBUDDY_API_KEY", "test-key")
			t.Setenv("BUILDBUDDY_URL", server.URL)

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
			output, err := captureTargetHistoryOutput(t, func() error { return cmd.RunE(cmd, nil) })
			if err != nil {
				t.Fatalf("run target history: %v", err)
			}
			if len(pageTokens) != 2 || pageTokens[0] != "" || pageTokens[1] != "page-2" {
				t.Errorf("history page tokens = %q, want empty then page-2", pageTokens)
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
				if got := len(history.GetTargetStatus()); got != 1 || history.GetTargetStatus()[0].GetInvocationId() != "keep-failure" {
					t.Errorf("JSON statuses = %v, want only keep-failure", history.GetTargetStatus())
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
			for _, excluded := range []string{"passed-only", "keep-pass", "old-failure", "other-failure"} {
				if strings.Contains(string(output), excluded) {
					t.Errorf("text output contains filtered invocation %q:\n%s", excluded, output)
				}
			}
			if !strings.Contains(string(output), "keep-failure") {
				t.Errorf("text output omits the matching page-two history:\n%s", output)
			}
		})
	}
}

func captureTargetHistoryOutput(t *testing.T, run func() error) ([]byte, error) {
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
