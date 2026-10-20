package main

import (
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"testing"
)

const subscriptionID = "00000000-0000-0000-0000-0000000000b1"

func subscriptionJSON(version int, cancelled bool) string {
	return fmt.Sprintf(`{"github":null,"id":%q,"inbox_id":"00000000-0000-0000-0000-0000000000b2",`+
		`"source":{"provider":"github","repository":"owner/repo","subject":{"kind":"pull_request","number":7},"events":null},`+
		`"idempotency_key":"k","version":%d,"cancelled":%t,"expires_at":"2026-01-08T00:00:00Z","last_success_at":null,`+
		`"error_kind":null,"error_since":null,"error_observed_at":null,"error":null,"retry_at":null}`, subscriptionID, version, cancelled)
}

type sentSubscription struct {
	DestinationRef map[string]string `json:"destination_ref"`
	SessionID      string            `json:"session_id"`
	IdempotencyKey string            `json:"idempotency_key"`
	Source         struct {
		Provider   string `json:"provider"`
		Repository string `json:"repository"`
		Subject    struct {
			Kind   string `json:"kind"`
			Number int    `json:"number"`
		} `json:"subject"`
		Events []eventFilter `json:"events"`
	} `json:"source"`
	LifetimeDays *int `json:"lifetime_days"`
}

func subscribed(t *testing.T, f *fake) []sentSubscription {
	t.Helper()
	var sent []sentSubscription
	for _, s := range f.requests("POST") {
		var body sentSubscription
		if err := json.Unmarshal(s.body, &body); err != nil {
			t.Fatal(err)
		}
		sent = append(sent, body)
	}
	return sent
}

func TestSubscribeToAPullRequest(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, subscriptionJSON(1, false)) })
	code, stdout, stderr := invoke(notificationsEnv(f.URL), "notifications", "subscribe", "github", "pr", "owner/repo#7", "--json")
	sent := subscribed(t, f)
	if code != 0 || len(sent) != 1 || decodeObject(t, stdout)["id"] != subscriptionID {
		t.Fatalf("exit %d, %d POSTs, stdout %q, stderr %q", code, len(sent), stdout, stderr)
	}
	got := sent[0]
	if got.DestinationRef["namespace"] != "test-ns" || got.DestinationRef["name"] != "test-sandbox" || got.DestinationRef["uid"] != "test-uid" || got.SessionID != "test-session" {
		t.Errorf("destination = %+v %q, want the environment's", got.DestinationRef, got.SessionID)
	}
	if got.Source.Provider != "github" || got.Source.Repository != "owner/repo" || got.Source.Subject.Kind != "pull_request" || got.Source.Subject.Number != 7 {
		t.Errorf("source = %+v", got.Source)
	}
	if got.Source.Events != nil || got.LifetimeDays != nil || got.IdempotencyKey == "" {
		t.Errorf("events %v, lifetime %v, key %q; want default events and lifetime and a derived key", got.Source.Events, got.LifetimeDays, got.IdempotencyKey)
	}
}

func TestSubscribeDerivesItsKeyFromTheSource(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, subscriptionJSON(1, false)) })
	run := func(args ...string) {
		invoke(notificationsEnv(f.URL), append([]string{"notifications", "subscribe", "github", "pr"}, args...)...)
	}
	run("owner/repo#7")
	run("owner/repo#7", "--lifetime-days", "30")
	run("owner/repo#7", "--event", "issue_comment:created", "--event", "check_run:requested,completed")
	run("owner/repo#7", "--event", "check_run:completed,requested", "--event", "issue_comment:created")
	run("owner/repo#8")
	run("owner/repo#7", "--key", "explicit")
	keys := []string{}
	for _, sent := range subscribed(t, f) {
		keys = append(keys, sent.IdempotencyKey)
	}
	same := func(i, j int) bool { return keys[i] == keys[j] }
	if !same(0, 1) || !same(2, 3) || same(0, 2) || same(0, 4) || keys[5] != "explicit" {
		t.Errorf("keys = %v; want one per source: lifetime and event order do not matter, events and number do", keys)
	}
}

func TestSubscribeEventsReplaceTheDefaults(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, subscriptionJSON(1, false)) })
	invoke(notificationsEnv(f.URL), "notifications", "subscribe", "github", "pr", "owner/repo#7",
		"--event", "pull_request_review", "--event", "check_run:completed,requested", "--lifetime-days", "14")
	got := subscribed(t, f)[0]
	events := fmt.Sprintf("%+v", got.Source.Events)
	if events != "[{Event:check_run Actions:[completed requested]} {Event:pull_request_review Actions:[]}]" || got.LifetimeDays == nil || *got.LifetimeDays != 14 {
		t.Errorf("events %s, lifetime %v", events, got.LifetimeDays)
	}
}

func TestSubscribeDestinationFlagWinsOverTheEnvironment(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, subscriptionJSON(1, false)) })
	flag := `{"destination_ref":{"namespace":"n","name":"s","uid":"u"},"session_id":"from-flag"}`
	invoke(notificationsEnv(f.URL), "notifications", "subscribe", "github", "pr", "owner/repo#7", "--destination", flag)
	env := notificationsEnv(f.URL)
	delete(env, "AGENTPLANE_DESTINATION")
	invoke(env, "notifications", "subscribe", "github", "pr", "owner/repo#7", "--destination", flag)
	code, _, _ := invoke(env, "notifications", "subscribe", "github", "pr", "owner/repo#7")
	sent := subscribed(t, f)
	if len(sent) != 2 || sent[0].SessionID != "from-flag" || sent[1].SessionID != "from-flag" || code != 2 {
		t.Errorf("%d POSTs; want the flag used with or without the variable, and exit 2 without either (got %d)", len(sent), code)
	}
}

func TestSubscribeToACancelledKeyExitsConflict(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, subscriptionJSON(3, true)) })
	code, stdout, stderr := invoke(notificationsEnv(f.URL), "notifications", "subscribe", "github", "pr", "owner/repo#7", "--json")
	if got := errorOf(t, stderr); code != 4 || stdout != "" || got.Code != "ALREADY_EXISTS" || got.Source != sourceClient {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 4 and empty stdout", code, stdout, got)
	}
}

func TestSubscribeWithAChangedLifetimeExitsConflict(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 409, `{"detail":"idempotency key reused with different parameters"}`)
	})
	code, stdout, stderr := invoke(notificationsEnv(f.URL), "notifications", "subscribe", "github", "pr", "owner/repo#7", "--json")
	if got := errorOf(t, stderr); code != 4 || stdout != "" || got.Code != "ALREADY_EXISTS" {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 4 ALREADY_EXISTS", code, stdout, got)
	}
}

func TestRenewPatchesWithTheVersionJustRead(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		if r.Method == http.MethodGet {
			reply(w, 200, subscriptionJSON(4, false))
			return
		}
		reply(w, 200, subscriptionJSON(5, false))
	})
	code, stdout, _ := invoke(notificationsEnv(f.URL), "notifications", "renew", subscriptionID, "--lifetime-days", "30", "--json")
	patches := f.requests("PATCH")
	if code != 0 || len(patches) != 1 || len(f.requests("GET")) != 1 || decodeObject(t, stdout)["version"] != float64(5) {
		t.Fatalf("exit %d, %d PATCHes, stdout %q", code, len(patches), stdout)
	}
	if body := decodeObject(t, string(patches[0].body)); body["version"] != float64(4) || body["lifetime_days"] != float64(30) || patches[0].path != "/v1/subscriptions/"+subscriptionID {
		t.Errorf("PATCH %s %s", patches[0].path, patches[0].body)
	}
}

func TestRenewOfAStaleVersionExitsConflict(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		if r.Method == http.MethodGet {
			reply(w, 200, subscriptionJSON(4, false))
			return
		}
		reply(w, 409, `{"detail":"version changed"}`)
	})
	code, stdout, stderr := invoke(notificationsEnv(f.URL), "notifications", "renew", subscriptionID, "--json")
	if got := errorOf(t, stderr); code != 4 || stdout != "" || got.Code != "ABORTED" {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 4 ABORTED", code, stdout, got)
	}
}

func TestCancelSubscriptionDeletesIt(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, subscriptionJSON(2, true)) })
	code, stdout, _ := invoke(notificationsEnv(f.URL), "notifications", "cancel", subscriptionID, "--json")
	deletes := f.requests("DELETE")
	if code != 0 || len(deletes) != 1 || deletes[0].path != "/v1/subscriptions/"+subscriptionID || decodeObject(t, stdout)["cancelled"] != true {
		t.Errorf("exit %d, DELETEs %+v, stdout %q", code, deletes, stdout)
	}
}

func TestSubscriptionOfAnotherAccountExitsNotFound(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		reply(w, 404, `{"detail":"resource not found or not owned by this account"}`)
	})
	if code, stdout, _ := invoke(notificationsEnv(f.URL), "notifications", "subscription", subscriptionID); code != 5 || stdout != "" {
		t.Errorf("exit %d, stdout %q; want exit 5", code, stdout)
	}
}

func TestSourcesNamesEachProviderAndJSONIsTheServicesOwn(t *testing.T) {
	const sources = `{"github":{"subscription_schema":{"title":"Subscribe"},"content":"Full GitHub webhook JSON; identity in event"},` +
		`"actions":{"subscription_schema":{"title":"Subscribe"},"content":"ActionEventView: sequence, state, at, actor"}}`
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, sources) })

	code, stdout, _ := invoke(notificationsEnv(f.URL), "notifications", "sources")
	want := "actions  ActionEventView: sequence, state, at, actor\ngithub  Full GitHub webhook JSON; identity in event"
	if code != 0 || strings.TrimSpace(stdout) != want {
		t.Errorf("exit %d, stdout %q; want exit 0 and %q", code, stdout, want)
	}
	code, stdout, _ = invoke(notificationsEnv(f.URL), "notifications", "sources", "--json")
	if code != 0 || strings.TrimSpace(stdout) != compact(t, sources) {
		t.Errorf("--json: exit %d, stdout %q; want exit 0 and the service's document", code, stdout)
	}
	for _, s := range f.all() {
		if s.method != http.MethodGet || s.path != "/v1/sources" {
			t.Errorf("request %s %s, want GET /v1/sources", s.method, s.path)
		}
	}
}

func TestSubscriptionsFollowPagesOf128(t *testing.T) {
	const total = 300
	ids := make([]string, total)
	for i := range ids {
		ids[i] = fmt.Sprintf("00000000-0000-0000-0000-%012d", i+1)
	}
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, s seen) {
		start := 0
		for i, id := range ids {
			if id == s.query.Get("after_id") {
				start = i + 1
			}
		}
		page := []string{}
		for _, id := range ids[start:min(start+128, total)] {
			page = append(page, strings.Replace(subscriptionJSON(1, false), subscriptionID, id, 1))
		}
		reply(w, 200, "["+strings.Join(page, ",")+"]")
	})
	for _, tc := range []struct {
		args []string
		want int
	}{{[]string{"--limit", "0"}, total}, {nil, 50}, {[]string{"--limit", "130"}, 130}, {[]string{"--after", ids[99], "--limit", "0"}, total - 100}} {
		code, stdout, _ := invoke(notificationsEnv(f.URL), append([]string{"notifications", "subscriptions", "--json"}, tc.args...)...)
		if got := jsonLines(t, stdout); code != 0 || len(got) != tc.want {
			t.Errorf("%v: exit %d with %d subscriptions, want %d", tc.args, code, len(got), tc.want)
		}
	}
}

func TestEgressRulesPassThroughAndProxyRefusalsAreAttributed(t *testing.T) {
	rules := `{"subject":{"namespace":"test-ns","name":"test-sa"},"policies":[{"name":"basic","rules":[{"hosts":["api.example.com"],"methods":{"values":["GET"]},"paths":null,"credential":null}]}]}`
	var refuse atomic.Bool
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
		if refuse.Load() {
			w.Header().Set(egressHeader, "denied; reason=no-rule")
			reply(w, 403, "")
			return
		}
		reply(w, 200, rules)
	})
	code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if code != 0 || strings.TrimSpace(stdout) != rules {
		t.Errorf("exit %d, stdout %q; want the rules as served", code, stdout)
	}
	if code, stdout, _ := invoke(egressEnv(f.URL), "egress", "rules"); code != 0 || !strings.Contains(stdout, "api.example.com") {
		t.Errorf("text mode: exit %d, stdout %q", code, stdout)
	}
	refuse.Store(true)
	code, _, stderr := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if got := errorOf(t, stderr); code != 1 || got.Source != sourceEgress || got.Reason == nil || *got.Reason != "no-rule" {
		t.Errorf("exit %d, envelope %+v; want exit 1 attributed to the egress proxy", code, got)
	}
}
