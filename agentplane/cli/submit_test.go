package main

import (
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"testing"
)

const createdID = "00000000-0000-0000-0000-0000000000c1"

// requestStore is an Action Service that keeps what is submitted under each idempotency key.
type requestStore struct {
	mu       sync.Mutex
	byKey    map[string]string
	atSubmit func(key string)
}

func newRequestStore(t *testing.T, state string, atSubmit func(key string)) (*fake, *requestStore) {
	store := &requestStore{byKey: map[string]string{}, atSubmit: atSubmit}
	return newFake(t, func(w http.ResponseWriter, r *http.Request, s seen) {
		store.mu.Lock()
		defer store.mu.Unlock()
		switch {
		case r.Method == http.MethodPost && r.URL.Path == "/v1/action-requests":
			var body struct {
				IdempotencyKey string `json:"idempotency_key"`
			}
			if err := json.Unmarshal(s.body, &body); err != nil {
				t.Errorf("submission is not JSON: %v", err)
			}
			if store.atSubmit != nil {
				store.atSubmit(body.IdempotencyKey)
			}
			if _, repeated := store.byKey[body.IdempotencyKey]; repeated {
				reply(w, 409, `{"detail":"idempotency key reused"}`)
				return
			}
			store.byKey[body.IdempotencyKey] = receiptJSON(createdID, state, body.IdempotencyKey)
			reply(w, 202, store.byKey[body.IdempotencyKey])
		case r.Method == http.MethodGet && r.URL.Path == "/v1/action-requests":
			found := []string{}
			if receipt, ok := store.byKey[s.query.Get("idempotency_key")]; ok {
				found = append(found, receipt)
			}
			reply(w, 200, "["+strings.Join(found, ",")+"]")
		default:
			reply(w, 404, `{"detail":"not found"}`)
		}
	}), store
}

func TestSubmitWritesItsGeneratedKeyBeforeThePostAndGetFindsIt(t *testing.T) {
	c := new(cli)
	var stderrAtPost, keyAtPost string
	f, _ := newRequestStore(t, "decision_pending", func(key string) {
		keyAtPost, stderrAtPost = key, c.stderr.String()
	})
	c = newCLI(actionsEnv(f.URL), "")
	if code := c.run("actions", "submit", "test-group/test-action", "--title", "t"); code != 0 {
		t.Fatalf("submit exit %d, stderr %q", code, c.stderr.String())
	}
	if keyAtPost == "" || !strings.Contains(stderrAtPost, keyAtPost) {
		t.Fatalf("the POST carried key %q but stderr held %q before it", keyAtPost, stderrAtPost)
	}
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "get", "--key", keyAtPost, "--json")
	got := decodeObject(t, stdout)
	if code != 0 || got["id"] != createdID || got["idempotency_key"] != keyAtPost {
		t.Errorf("get --key %q: exit %d, %v; want the submitted request", keyAtPost, code, got)
	}
}

func TestSubmitAnnouncesAGeneratedKeyAsJSONUnderJSON(t *testing.T) {
	f, _ := newRequestStore(t, "decision_pending", nil)
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "submit", "test-group/test-action", "--title", "t", "--json")
	var announced struct {
		Key string `json:"idempotency_key"`
	}
	if err := json.Unmarshal([]byte(stderr), &announced); err != nil || code != 0 || decodeObject(t, stdout)["idempotency_key"] != announced.Key {
		t.Errorf("exit %d, stderr %q: want the key as a JSON object matching the receipt", code, stderr)
	}
}

func TestSubmitWithAKeyAnnouncesNothing(t *testing.T) {
	f, _ := newRequestStore(t, "decision_pending", nil)
	_, _, stderr := invoke(actionsEnv(f.URL), "actions", "submit", "test-group/test-action", "--title", "t", "--key", "mine")
	if stderr != "" {
		t.Errorf("stderr = %q, want nothing", stderr)
	}
}

func TestSubmitSendsTheRequestAsGiven(t *testing.T) {
	file := filepath.Join(t.TempDir(), "arguments.json")
	if err := os.WriteFile(file, []byte(`{ "from": "file" }`), 0o600); err != nil {
		t.Fatal(err)
	}
	cases := []struct {
		name      string
		arguments []string
		stdin     string
		want      string
	}{
		{"absent", nil, "", `{}`},
		{"literal", []string{"--arguments", `{"n": 12345678901234567890, "x": [1, 2]}`}, "", `{"n":12345678901234567890,"x":[1,2]}`},
		{"file", []string{"--arguments", "@" + file}, "", `{"from":"file"}`},
		{"stdin", []string{"--arguments", "-"}, `{"from": "stdin"}`, `{"from":"stdin"}`},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f, _ := newRequestStore(t, "decision_pending", nil)
			c := newCLI(actionsEnv(f.URL), tc.stdin)
			args := append([]string{"actions", "submit", "test-group/test-action", "--title", "the title", "--description", "the detail", "--key", "k"}, tc.arguments...)
			if code := c.run(args...); code != 0 {
				t.Fatalf("exit %d, stderr %q", code, c.stderr.String())
			}
			var sent struct {
				IdempotencyKey string          `json:"idempotency_key"`
				Action         map[string]any  `json:"action"`
				Arguments      json.RawMessage `json:"arguments"`
				Title          string          `json:"title"`
				Description    *string         `json:"description"`
			}
			posts := f.requests("POST")
			if len(posts) != 1 {
				t.Fatalf("%d POSTs, want 1", len(posts))
			}
			if err := json.Unmarshal(posts[0].body, &sent); err != nil {
				t.Fatal(err)
			}
			if string(sent.Arguments) != tc.want || sent.Action["group"] != "test-group" || sent.Action["name"] != "test-action" ||
				sent.Title != "the title" || sent.Description == nil || *sent.Description != "the detail" || sent.IdempotencyKey != "k" {
				t.Errorf("sent %s", posts[0].body)
			}
		})
	}
}

func TestSubmitOmitsAnAbsentDescription(t *testing.T) {
	f, _ := newRequestStore(t, "decision_pending", nil)
	invoke(actionsEnv(f.URL), "actions", "submit", "test-group/test-action", "--title", "t")
	if sent := decodeObject(t, string(f.requests("POST")[0].body)); sent["description"] != nil {
		t.Errorf("description = %v, want it left out", sent["description"])
	}
}

func TestRepeatedKeyExitsConflictWithoutALookup(t *testing.T) {
	f, _ := newRequestStore(t, "decision_pending", nil)
	invoke(actionsEnv(f.URL), "actions", "submit", "test-group/test-action", "--title", "t", "--key", "same")
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "submit", "test-group/test-action", "--title", "t", "--key", "same", "--json")
	got := errorOf(t, stderr)
	if code != 4 || stdout != "" || got.Code != "ALREADY_EXISTS" || len(f.requests("GET")) != 0 || len(f.requests("POST")) != 2 {
		t.Errorf("exit %d, stdout %q, envelope %+v, %d GETs; want exit 4 ALREADY_EXISTS and no lookup", code, stdout, got, len(f.requests("GET")))
	}
}

func TestSubmitRefusalsAreFinalAndLookNothingUp(t *testing.T) {
	for status, wantExit := range map[int]int{400: 1, 401: 1, 403: 1, 404: 5, 422: 1, 429: 3} {
		f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, status, `{"detail":"no"}`) })
		code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t", "--key", "k")
		if code != wantExit || stdout != "" || f.count() != 1 {
			t.Errorf("status %d: exit %d, stdout %q, %d requests; want exit %d and the one POST", status, code, stdout, f.count(), wantExit)
		}
	}
}

func TestLostSubmitResponseSendsOnePostAndAtMostOneLookup(t *testing.T) {
	lost := map[string]func(http.ResponseWriter){
		"connection closed after the request": drop,
		"500":                                 func(w http.ResponseWriter) { reply(w, 500, `{"detail":"boom"}`) },
		"502":                                 func(w http.ResponseWriter) { reply(w, 502, "") },
		"503":                                 func(w http.ResponseWriter) { reply(w, 503, `{"detail":"draining"}`) },
		"504":                                 func(w http.ResponseWriter) { reply(w, 504, "") },
	}
	lookups := map[string]struct {
		answer   func(http.ResponseWriter)
		wantExit int
	}{
		"found":         {func(w http.ResponseWriter) { reply(w, 200, "["+receiptJSON(createdID, "decision_pending", "k")+"]") }, 0},
		"found nothing": {func(w http.ResponseWriter) { reply(w, 200, "[]") }, exitSubmitLost},
		"lookup fails":  {func(w http.ResponseWriter) { reply(w, 503, "") }, exitSubmitLost},
	}
	for lostName, lose := range lost {
		for lookupName, lookup := range lookups {
			t.Run(lostName+"/"+lookupName, func(t *testing.T) {
				f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
					if r.Method == http.MethodPost {
						lose(w)
						return
					}
					lookup.answer(w)
				})
				c := newCLI(actionsEnv(f.URL), "")
				code := c.run("actions", "submit", "g/a", "--title", "t", "--key", "k", "--json")
				if code != lookup.wantExit || len(f.requests("POST")) != 1 || len(f.requests("GET")) != 1 {
					t.Fatalf("exit %d with %d POSTs and %d GETs; want exit %d, one POST and one GET", code, len(f.requests("POST")), len(f.requests("GET")), lookup.wantExit)
				}
				if got := f.requests("GET")[0].query.Get("idempotency_key"); got != "k" {
					t.Errorf("the lookup was for key %q, want the submitted key", got)
				}
				if lookup.wantExit == 0 {
					if got := decodeObject(t, c.stdout.String()); got["id"] != createdID {
						t.Errorf("stdout = %q, want the found receipt", c.stdout.String())
					}
				} else if c.stdout.String() != "" {
					t.Errorf("stdout = %q, want nothing after exit %d", c.stdout.String(), code)
				}
			})
		}
	}
}

func TestSubmitWithAnUnreadableAnswerLooksTheKeyUpOnce(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		if r.Method == http.MethodPost {
			reply(w, 202, `<html>not the Action Service</html>`)
			return
		}
		reply(w, 200, "["+receiptJSON(createdID, "decision_pending", "k")+"]")
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t", "--key", "k", "--json")
	if code != 0 || decodeObject(t, stdout)["id"] != createdID || len(f.requests("POST")) != 1 || len(f.requests("GET")) != 1 {
		t.Errorf("exit %d, stdout %q, %d POSTs, %d GETs; want the looked-up receipt after one of each", code, stdout, len(f.requests("POST")), len(f.requests("GET")))
	}
}

func TestSubmitThatNeverReachedTheServiceIsTransientAndSendsNothingTwice(t *testing.T) {
	f := newFake(t, func(http.ResponseWriter, *http.Request, seen) {})
	f.Close()
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t")
	if code != 3 || stdout != "" || !strings.Contains(stderr, "idempotency key: ") {
		t.Errorf("exit %d, stdout %q, stderr %q; want exit 3 with the key announced", code, stdout, stderr)
	}
}

func TestSubmitWaitFollowsTheRequestToItsOutcome(t *testing.T) {
	for state, wantExit := range map[string]int{"succeeded": 0, "denied": 11, "failed": 12, "cancelled": 13, "execution_unknown": 14} {
		t.Run(state, func(t *testing.T) {
			f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
				if r.Method == http.MethodPost {
					reply(w, 202, receiptJSON(createdID, "decision_pending", "k"))
					return
				}
				reply(w, 200, receiptJSON(createdID, state, "k"))
			})
			code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t", "--key", "k", "--wait", "--json")
			if got := jsonLines(t, stdout); code != wantExit || len(got) != 1 || got[0]["state"] != state {
				t.Errorf("exit %d, stdout %q; want exit %d and the %s receipt alone", code, stdout, wantExit, state)
			}
			waits := f.requests("GET")
			if len(waits) != 1 || waits[0].path != "/v1/action-requests/"+createdID || waits[0].query.Get("wait_until") != "terminal" || waits[0].query.Get("wait_seconds") == "" {
				t.Errorf("GETs %+v, want one server-side wait for the terminal state", waits)
			}
		})
	}
}

func TestSubmitWaitUntilDecisionStopsAtTheDecision(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		if r.Method == http.MethodPost {
			reply(w, 202, receiptJSON(createdID, "decision_pending", "k"))
			return
		}
		reply(w, 200, receiptJSON(createdID, "running", "k"))
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t", "--key", "k", "--wait", "--until", "decision", "--json")
	if got := decodeObject(t, stdout); code != 0 || got["state"] != "running" || f.requests("GET")[0].query.Get("wait_until") != "decision" {
		t.Errorf("exit %d, stdout %q; want exit 0 on the allowed Action", code, stdout)
	}
}

func TestSubmitWaitDoesNotReadAReceiptThatAlreadyHolds(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 202, receiptJSON(createdID, "denied", "k"))
	})
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t", "--key", "k", "--wait")
	if code != 11 || len(f.requests("GET")) != 0 {
		t.Errorf("exit %d with %d GETs, want exit 11 and none", code, len(f.requests("GET")))
	}
}

func TestSubmitWithoutWaitExitsZeroOnceRecorded(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 202, receiptJSON(createdID, "denied", "k"))
	})
	code, stdout, _ := invoke(actionsEnv(f.URL), "actions", "submit", "g/a", "--title", "t", "--key", "k", "--json")
	if code != 0 || decodeObject(t, stdout)["state"] != "denied" {
		t.Errorf("exit %d, stdout %q; want exit 0 and the receipt", code, stdout)
	}
}
