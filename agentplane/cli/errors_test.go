package main

import (
	"maps"
	"net/http"
	"strings"
	"testing"
)

const missingID = "00000000-0000-0000-0000-00000000dead"

func TestHTTPFailuresMapToExitCodes(t *testing.T) {
	egress := func(reason string) map[string]string {
		return map[string]string{egressHeader: "denied; reason=" + reason}
	}
	sidecar := func(reason string) map[string]string { return map[string]string{sidecarHeader: "reason=" + reason} }
	cases := []struct {
		name       string
		status     int
		headers    map[string]string
		body       string
		wantExit   int
		wantCode   string
		wantSource string
		wantReason string
	}{
		{"bad request", 400, nil, `{"detail":"bad"}`, 1, "INVALID_ARGUMENT", sourceService, ""},
		{"validation", 422, nil, `{"detail":[{"loc":["body","title"],"msg":"too long"}]}`, 1, "INVALID_ARGUMENT", sourceService, ""},
		{"unauthenticated", 401, nil, `{"detail":"invalid workload bearer"}`, 1, "UNAUTHENTICATED", sourceService, ""},
		{"service denies", 403, nil, `{"detail":"no"}`, 1, "PERMISSION_DENIED", sourceService, ""},
		{"proxy denies", 403, egress("no-rule"), "", 1, "PERMISSION_DENIED", sourceEgress, "no-rule"},
		{"proxy cannot find the placeholder", 403, egress("placeholder-unresolved"), "", 1, "PERMISSION_DENIED", sourceEgress, "placeholder-unresolved"},
		{"sidecar bad request", 400, sidecar("bad-request"), "", 1, "INVALID_ARGUMENT", sourceSidecar, "bad-request"},
		{"not found", 404, nil, `{"detail":"action request not found"}`, 5, "NOT_FOUND", sourceService, ""},
		{"conflict", 409, nil, `{"detail":"stale"}`, 4, "ABORTED", sourceService, ""},
		{"rate limited", 429, map[string]string{"Retry-After": "7"}, `{"detail":"slow down"}`, 3, "RESOURCE_EXHAUSTED", sourceService, ""},
		{"proxy could not decide", 502, egress("unavailable"), "", 3, "UNAVAILABLE", sourceEgress, "unavailable"},
		{"sidecar cannot reach the proxy", 502, sidecar("proxy-unreachable"), "", 3, "UNAVAILABLE", sourceSidecar, "proxy-unreachable"},
		{"sidecar has no token", 503, sidecar("token-unavailable"), "", 3, "UNAVAILABLE", sourceSidecar, "token-unavailable"},
		{"draining", 503, nil, `{"detail":"Action Service is draining"}`, 3, "UNAVAILABLE", sourceService, ""},
		{"gateway timeout", 504, nil, "", 3, "DEADLINE_EXCEEDED", sourceService, ""},
		{"server error", 500, nil, `{"detail":"boom"}`, 1, "INTERNAL", sourceService, ""},
		{"gRPC status body", 401, nil, `{"code":16,"message":"unauthenticated","details":[]}`, 1, "UNAUTHENTICATED", sourceService, ""},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) {
				for name, value := range tc.headers {
					w.Header().Set(name, value)
				}
				reply(w, tc.status, tc.body)
			})
			code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "get", missingID, "--json")
			if code != tc.wantExit || stdout != "" {
				t.Fatalf("exit %d, stdout %q; want exit %d and empty stdout", code, stdout, tc.wantExit)
			}
			got := errorOf(t, stderr)
			if got.Code != tc.wantCode || got.Source != tc.wantSource || got.HTTPStatus == nil || *got.HTTPStatus != tc.status {
				t.Errorf("envelope = %+v, want %s from %s with http_status %d", got, tc.wantCode, tc.wantSource, tc.status)
			}
			if (tc.wantReason == "") != (got.Reason == nil) || (got.Reason != nil && *got.Reason != tc.wantReason) {
				t.Errorf("reason = %v, want %q", got.Reason, tc.wantReason)
			}
			if tc.status == 429 && (got.RetryAfterSeconds == nil || *got.RetryAfterSeconds != 7) {
				t.Errorf("retry_after_seconds = %v, want 7", got.RetryAfterSeconds)
			}
			if got.Message == "" {
				t.Error("the envelope has no message")
			}
		})
	}
}

func TestUnreachableServiceExitsTransient(t *testing.T) {
	f := newFake(t, func(http.ResponseWriter, *http.Request, seen) {})
	f.Close()
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "get", missingID, "--json")
	got := errorOf(t, stderr)
	if code != 3 || stdout != "" || got.Code != "UNAVAILABLE" || got.Source != sourceClient || got.HTTPStatus != nil {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 3 with UNAVAILABLE from the client", code, stdout, got)
	}
}

func TestDeadlineExitsTransient(t *testing.T) {
	f := newFake(t, func(_ http.ResponseWriter, r *http.Request, _ seen) { <-r.Context().Done() })
	code, stdout, stderr := invoke(actionsEnv(f.URL), "actions", "get", missingID, "--json", "--timeout", "0.3")
	got := errorOf(t, stderr)
	if code != 3 || stdout != "" || got.Code != "DEADLINE_EXCEEDED" || got.Source != sourceClient {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 3 with DEADLINE_EXCEEDED from the client", code, stdout, got)
	}
}

func TestCredentialIsSentAsBearerAndNeverPrinted(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 403, `{"detail":"no"}`) })
	for _, args := range [][]string{{"actions", "get", missingID}, {"actions", "get", missingID, "--json"}} {
		code, stdout, stderr := invoke(actionsEnv(f.URL), args...)
		if code != 1 || strings.Contains(stdout+stderr, testCredential) {
			t.Errorf("%v: exit %d; the credential appears in the output: %q %q", args, code, stdout, stderr)
		}
	}
	for _, s := range f.requests("GET") {
		if s.authorization != "Bearer "+testCredential {
			t.Errorf("Authorization = %q, want the bearer placeholder", s.authorization)
		}
	}
}

func TestRedirectIsNotFollowed(t *testing.T) {
	elsewhere := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, `{}`) })
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		http.Redirect(w, r, elsewhere.URL, http.StatusFound)
	})
	code, _, _ := invoke(actionsEnv(f.URL), "actions", "get", missingID)
	if code != 1 || elsewhere.count() != 0 {
		t.Errorf("exit %d, %d requests followed the redirect; want exit 1 and none", code, elsewhere.count())
	}
}

func TestEachGroupNeedsOnlyItsOwnConfiguration(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		if r.URL.Path == "/v1/rules" {
			reply(w, 200, `{"subject":{"namespace":"test-ns","name":"test-sa"},"policies":[]}`)
			return
		}
		reply(w, 200, `[]`)
	})
	groups := []struct {
		env     map[string]string
		args    []string
		missing string
	}{
		{actionsEnv(f.URL), []string{"actions", "groups"}, "AGENTPLANE_ACTIONS_URL"},
		{notificationsEnv(f.URL), []string{"notifications", "subscriptions"}, "AGENTPLANE_NOTIFICATIONS_URL"},
		{egressEnv(f.URL), []string{"egress", "rules"}, "AGENTPLANE_EGRESS_URL"},
	}
	for i, configured := range groups {
		for j, command := range groups {
			before := f.count()
			code, _, stderr := invoke(configured.env, append(command.args, "--json")...)
			if i == j {
				if code != 0 || f.count() != before+1 {
					t.Errorf("%s with only its own configuration: exit %d, stderr %q", command.args[0], code, stderr)
				}
				continue
			}
			got := errorOf(t, stderr)
			if code != 2 || got.Code != "INVALID_ARGUMENT" || !strings.Contains(got.Message, command.missing) || f.count() != before {
				t.Errorf("%s without its configuration: exit %d, envelope %+v, %d requests; want exit 2 naming %s and no request",
					command.args[0], code, got, f.count()-before, command.missing)
			}
		}
	}
}

func TestUsageErrorsExitBeforeAnyRequest(t *testing.T) {
	f := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, `[]`) })
	env := actionsEnv(f.URL)
	maps.Copy(env, notificationsEnv(f.URL))
	cases := [][]string{
		{"actions", "no-such-command"},
		{"actions", "get"},
		{"actions", "get", missingID, "--key", "k"},
		{"actions", "get", "--no-such-flag"},
		{"actions", "show", "no-slash"},
		{"actions", "submit", "g/a"},
		{"actions", "submit", "g/a", "--title", "t", "--arguments", "{not json"},
		{"actions", "submit", "g/a", "--title", "t", "--until", "decision"},
		{"actions", "wait", missingID, "--until", "never"},
		{"actions", "requests", "--limit", "-1"},
		{"actions", "groups", "--timeout", "soon"},
		{"notifications", "subscribe", "github", "pr", "not-a-pull-request"},
		{"notifications", "subscribe", "github", "pr", "o/r#1", "--event", ":created"},
		{"actions"},
		{},
	}
	for _, args := range cases {
		code, stdout, stderr := invoke(env, args...)
		if code != exitUsage || stdout != "" || f.count() != 0 {
			t.Errorf("%v: exit %d, stdout %q, %d requests (stderr %q); want exit 2, no output, no request", args, code, stdout, f.count(), stderr)
		}
	}
}
