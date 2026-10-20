package main

import (
	"bytes"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"testing"
)

func TestSucceededActionExitsZeroWhateverItsResultSays(t *testing.T) {
	results := map[string]string{
		"a script that exited 3": `{"exit":{"exit_code":3},"stdout":"","stderr":"boom"}`,
		"an MCP tool error":      `{"content":[{"type":"text","text":"it broke"}],"isError":true}`,
		"no result":              `null`,
	}
	for name, result := range results {
		t.Run(name, func(t *testing.T) {
			receipt := receiptWithExecution(waitedID, "succeeded", "k", succeededWith(result))
			f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
				if r.Method == http.MethodPost {
					reply(w, 202, receiptJSON(waitedID, "decision_pending", "k"))
					return
				}
				reply(w, 200, receipt)
			})
			for _, args := range [][]string{
				{"actions", "result", waitedID},
				{"actions", "wait", waitedID},
				{"actions", "submit", "g/a", "--title", "t", "--key", "k", "--wait"},
			} {
				code, stdout, _ := invoke(actionsEnv(f.URL), append(args, "--json")...)
				want := receipt
				if args[1] == "result" {
					want = result
				}
				if code != 0 || strings.TrimSpace(stdout) != compact(t, want) {
					t.Errorf("%v: exit %d, stdout %q; want exit 0 and %s untouched", args, code, stdout, want)
				}
			}
		})
	}
}

func TestResultPrintsTextBlocksAndOtherwiseIndentedJSON(t *testing.T) {
	cases := []struct{ name, result, want string }{
		{"text blocks", `{"content":[{"type":"text","text":"one"},{"type":"image","data":"x"},{"type":"text","text":"two"}],"isError":false}`, "one\ntwo"},
		{"no text blocks", `{"exit":{"exit_code":0}}`, "{\n  \"exit\": {\n    \"exit_code\": 0\n  }\n}"},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
				reply(w, 200, receiptWithExecution(waitedID, "succeeded", "k", succeededWith(tc.result)))
			})
			code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "result", waitedID)
			if code != 0 || strings.TrimSpace(stdout) != tc.want {
				t.Errorf("exit %d, stdout %q; want %q", code, stdout, tc.want)
			}
		})
	}
}

func TestResultOfAnUnsuccessfulActionPrintsTheReceipt(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, receiptJSON(waitedID, "denied", "k"))
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "result", waitedID, "--json")
	if got := decodeObject(t, stdout); code != exitDenied || got["id"] != waitedID {
		t.Errorf("exit %d, stdout %q; want exit 11 and the receipt", code, stdout)
	}
}

func TestCancelExitsByOutcome(t *testing.T) {
	cases := []struct {
		outcome  string
		wantExit int
	}{
		{"cancelled", 0}, {"already_cancelled", 0}, {"too_late", exitTooLate}, {"already_finished", exitAlreadyFinished},
	}
	for _, tc := range cases {
		answer := fmt.Sprintf(`{"outcome":%q,"request":%s}`, tc.outcome, receiptJSON(waitedID, "cancelled", "k"))
		f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, answer) })
		code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "cancel", waitedID, "--json")
		posts := f.requests("POST")
		if code != tc.wantExit || strings.TrimSpace(stdout) != compact(t, answer) || len(posts) != 1 || posts[0].path != "/v1/action-requests/"+waitedID+"/cancel" {
			t.Errorf("%s: exit %d, stdout %q, POSTs %+v; want exit %d and the result untouched", tc.outcome, code, stdout, posts, tc.wantExit)
		}
	}
}

func TestCancelOfAnUnknownRequestExitsNotFound(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 404, `{"detail":"action request not found"}`)
	})
	if code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "cancel", missingID); code != 5 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestGetExitsZeroForAnyState(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, receiptJSON(waitedID, "denied", "k"))
	})
	if code, _, _ := invoke(actionsEnv(f.URL), "actions", "get", waitedID); code != 0 {
		t.Errorf("get of a denied request: exit %d, want 0", code)
	}
}

func TestGetByKeyThatMatchesNothingExitsNotFound(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, "[]") })
	if code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "get", "--key", "nobody"); code != 5 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestJSONOutputIsTheServicesObjectsUnmodified(t *testing.T) {
	objects := []string{
		`{"id":"a","n":12345678901234567890,"extra":{"keep":[1,2.50,null],"unicode":"é<&>"}}`,
		`{"id":"b","z":1,"a":2}`,
	}
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, "[\n  "+objects[0]+" ,\n  "+objects[1]+"\n]")
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "requests", "--json")
	want := compact(t, objects[0]) + "\n" + compact(t, objects[1]) + "\n"
	if code != 0 || stdout != want {
		t.Errorf("exit %d, stdout %q; want one object per line, as sent:\n%s", code, stdout, want)
	}
}

func TestUnaryJSONOutputIsOneValue(t *testing.T) {
	object := "{\n \"subject\": {\"namespace\":\"n\",\"name\":\"s\"},\n \"policies\": []\n}"
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, object) })
	code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if code != 0 || stdout != compact(t, object)+"\n" {
		t.Errorf("exit %d, stdout %q", code, stdout)
	}
}

func TestListFlattensTheCatalogIntoActions(t *testing.T) {
	groups := `[{"key":"g1","title":"One","description":"","executor_kind":"mcp","executor_description":"","available":true,"health":null,
		"actions":[{"group":"g1","name":"a","description":"first","input_schema":{}},{"group":"g1","name":"b","description":"second","input_schema":{}}]},
		{"key":"g2","title":"Two","description":"","executor_kind":"pod","executor_description":"","available":true,"health":null,
		"actions":[{"group":"g2","name":"c","description":"third","input_schema":{}}]}]`
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, groups) })
	for _, tc := range []struct {
		args     []string
		wantExit int
		want     []string
	}{
		{[]string{"list"}, 0, []string{"a", "b", "c"}},
		{[]string{"list", "g2"}, 0, []string{"c"}},
		{[]string{"list", "missing"}, 5, nil},
	} {
		code, stdout, _ := invoke(actionsEnv(f.URL), append(append([]string{"actions"}, tc.args...), "--json")...)
		var names []string
		for _, action := range jsonLines(t, stdout) {
			names = append(names, action["name"].(string))
		}
		if code != tc.wantExit || strings.Join(names, ",") != strings.Join(tc.want, ",") {
			t.Errorf("%v: exit %d, actions %v; want exit %d and %v", tc.args, code, names, tc.wantExit, tc.want)
		}
	}
	if code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "groups", "--json"); code != 0 || len(jsonLines(t, stdout)) != 2 {
		t.Errorf("groups: exit %d, stdout %q; want one line per group", code, stdout)
	}
}

func TestShowAndPolicyReadTheirRoutes(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 200, `{"group":"g","name":"a","description":"d","input_schema":{}}`)
	})
	invoke(actionsEnv(f.URL), "actions", "show", "g/a")
	invoke(actionsEnv(f.URL), "actions", "policy")
	paths := []string{}
	for _, s := range f.requests("GET") {
		paths = append(paths, s.path)
	}
	if strings.Join(paths, " ") != "/v1/action-groups/g/actions/a /v1/action-policy" {
		t.Errorf("GETs %v", paths)
	}
}

// pagedRequests serves total requests, newest first, honoring limit and after_id.
func pagedRequests(t *testing.T, total int) *fake {
	ids := make([]string, total)
	for i := range ids {
		ids[i] = fmt.Sprintf("00000000-0000-0000-0000-%012d", total-i)
	}
	return newFake(t, func(w http.ResponseWriter, _ *http.Request, s seen) {
		start := 0
		if after := s.query.Get("after_id"); after != "" {
			start = -1
			for i, id := range ids {
				if id == after {
					start = i + 1
				}
			}
			if start < 0 {
				reply(w, 404, `{"detail":"action request not found"}`)
				return
			}
		}
		limit := total
		if text := s.query.Get("limit"); text != "" {
			limit, _ = strconv.Atoi(text)
		}
		page := []string{}
		for _, id := range ids[start:min(start+limit, total)] {
			page = append(page, receiptJSON(id, "succeeded", "k"))
		}
		reply(w, 200, "["+strings.Join(page, ",")+"]")
	})
}

func TestRequestsFollowPagesUntilTheLimit(t *testing.T) {
	f := pagedRequests(t, 300)
	for _, tc := range []struct {
		limit string
		want  int
	}{{"0", 300}, {"200", 200}, {"3", 3}, {"500", 300}} {
		code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "requests", "--limit", tc.limit, "--json")
		if got := jsonLines(t, stdout); code != 0 || len(got) != tc.want {
			t.Errorf("--limit %s: exit %d with %d items, want %d", tc.limit, code, len(got), tc.want)
		}
	}
	for _, s := range f.requests("GET") {
		if limit, _ := strconv.Atoi(s.query.Get("limit")); limit < 1 || limit > 128 {
			t.Errorf("asked the server for a page of %q, want 1-128", s.query.Get("limit"))
		}
	}
}

func TestRequestsDefaultToFiftyAndResumeAfterTheLastOne(t *testing.T) {
	f := pagedRequests(t, 120)
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "requests", "--json")
	first := jsonLines(t, stdout)
	if code != 0 || len(first) != 50 {
		t.Fatalf("exit %d with %d items, want 50", code, len(first))
	}
	last := first[len(first)-1]["id"].(string)
	code, stdout, _ = invoke(actionsEnv(f.URL), "actions", "requests", "--after", last, "--limit", "0", "--json")
	rest := jsonLines(t, stdout)
	if code != 0 || len(rest) != 70 || rest[0]["id"] == last {
		t.Errorf("--after %s: exit %d with %d items, want the remaining 70", last, code, len(rest))
	}
}

func TestRequestsTextModeSaysWhereToResume(t *testing.T) {
	f := pagedRequests(t, 10)
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "requests", "--limit", "4")
	if code != 0 || len(strings.Split(strings.TrimSpace(stdout), "\n")) != 4 || !strings.Contains(stderr, "--after 00000000-0000-0000-0000-000000000007") {
		t.Errorf("exit %d, stdout %q, stderr %q; want four lines and the resume hint on stderr", code, stdout, stderr)
	}
}

func TestRequestsAfterAnUnknownIDExitsNotFound(t *testing.T) {
	f := pagedRequests(t, 3)
	if code, _, _ := invoke(actionsEnv(f.URL), "actions", "requests", "--after", "00000000-0000-0000-0000-00000000beef"); code != 5 {
		t.Errorf("exit %d, want 5", code)
	}
}

func TestRequestsPassStateFilters(t *testing.T) {
	f := pagedRequests(t, 1)
	invoke(actionsEnv(f.URL), "actions", "requests", "--state", "running", "--state", "decision_pending")
	if got := f.requests("GET")[0].query["state"]; strings.Join(got, ",") != "running,decision_pending" {
		t.Errorf("state = %v", got)
	}
}

func TestEventsFollowPagesFromASequence(t *testing.T) {
	const total = 200
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, s seen) {
		after, _ := strconv.Atoi(s.query.Get("after_sequence"))
		limit, _ := strconv.Atoi(s.query.Get("limit"))
		events := []string{}
		for sequence := after + 1; sequence <= min(after+limit, total); sequence++ {
			events = append(events, fmt.Sprintf(`{"sequence":%d,"state":"running","at":"2026-01-01T00:00:00Z","actor":null}`, sequence))
		}
		reply(w, 200, "["+strings.Join(events, ",")+"]")
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "events", waitedID, "--after", "10", "--limit", "0", "--json")
	got := jsonLines(t, stdout)
	if code != 0 || len(got) != total-10 || got[0]["sequence"] != float64(11) || got[len(got)-1]["sequence"] != float64(total) {
		t.Errorf("exit %d with %d events, want 190 from sequence 11", code, len(got))
	}
}

// compact is a JSON text without insignificant whitespace.
func compact(t *testing.T, text string) string {
	t.Helper()
	var out bytes.Buffer
	if err := json.Compact(&out, []byte(text)); err != nil {
		t.Fatal(err)
	}
	return out.String()
}
