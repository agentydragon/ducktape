package main

import (
	"context"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"os"
	"os/exec"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/action_service/action_servicepb"
	rpcCode "google.golang.org/genproto/googleapis/rpc/code"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

func TestGRPCFailuresMapToExitCodes(t *testing.T) {
	cases := []struct {
		name     string
		code     codes.Code
		wantExit int
	}{
		{"invalid argument", codes.InvalidArgument, exitRefused},
		{"unauthenticated", codes.Unauthenticated, exitRefused},
		{"permission denied", codes.PermissionDenied, exitRefused},
		{"not found", codes.NotFound, exitNotFound},
		{"already exists", codes.AlreadyExists, exitConflict},
		{"aborted", codes.Aborted, exitConflict},
		{"rate limited", codes.ResourceExhausted, exitTransient},
		{"unavailable", codes.Unavailable, exitTransient},
		{"deadline exceeded", codes.DeadlineExceeded, exitTransient},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			fake, endpoint := newActionFake(t)
			fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
				return nil, status.Error(tc.code, "test service refusal")
			}
			code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "get", missingID, "--json")
			if code != tc.wantExit || stdout != "" || fake.count("GetActionRequest") != 1 {
				t.Fatalf("exit %d, stdout %q, calls %+v; want exit %d after one RPC", code, stdout, fake.callLog(), tc.wantExit)
			}
			got := errorOf(t, stderr)
			if got.Code != rpcCode.Code_name[int32(tc.code)] || got.Source != sourceService || got.HTTPStatus != nil || got.Message != "test service refusal" {
				t.Errorf("envelope %+v; want %s from service without an HTTP status", got, tc.code)
			}
		})
	}
}

func TestGRPCRetryInfoBecomesRetryAfterSeconds(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		st, err := status.New(codes.ResourceExhausted, "slow down").WithDetails(&errdetails.RetryInfo{RetryDelay: durationpb.New(7 * time.Second)})
		if err != nil {
			return nil, err
		}
		return nil, st.Err()
	}
	code, _, stderr := invoke(actionsEnv(endpoint), "actions", "get", missingID, "--json")
	got := errorOf(t, stderr)
	if code != exitTransient || got.Code != "RESOURCE_EXHAUSTED" || got.RetryAfterSeconds == nil || *got.RetryAfterSeconds != 7 {
		t.Errorf("exit %d, envelope %+v; want transient with a 7-second retry delay", code, got)
	}
}

func TestUnreachableServiceExitsTransient(t *testing.T) {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	endpoint := "http://" + listener.Addr().String()
	_ = listener.Close()
	code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "get", missingID, "--json")
	got := errorOf(t, stderr)
	if code != exitTransient || stdout != "" || got.Code != "UNAVAILABLE" || got.Source != sourceClient || got.HTTPStatus != nil {
		t.Errorf("exit %d, stdout %q, envelope %+v; want exit 3 with UNAVAILABLE from the client", code, stdout, got)
	}
}

func TestDeadlineExitsTransient(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(ctx context.Context, _ *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		<-ctx.Done()
		return nil, status.FromContextError(ctx.Err()).Err()
	}
	code, stdout, stderr := invoke(actionsEnv(endpoint), "actions", "get", missingID, "--json", "--timeout", "0.2")
	got := errorOf(t, stderr)
	if code != exitTransient || stdout != "" || got.Code != "DEADLINE_EXCEEDED" || fake.count("GetActionRequest") != 1 {
		t.Errorf("exit %d, stdout %q, envelope %+v; want deadline exceeded after one RPC", code, stdout, got)
	}
}

func proxySubprocessEnv(values map[string]string) []string {
	current := make([]string, 0, len(os.Environ())+len(values))
	for _, item := range os.Environ() {
		name, _, _ := strings.Cut(item, "=")
		if _, replaced := values[name]; !replaced {
			current = append(current, item)
		}
	}
	for name, value := range values {
		current = append(current, name+"="+value)
	}
	return current
}

func runProxyChild(t *testing.T, testName, endpoint, proxyURL string) {
	runProxyChildWithProxies(t, testName, endpoint, proxyURL, proxyURL)
}

func runProxyChildWithProxies(t *testing.T, testName, endpoint, httpProxyURL, httpsProxyURL string) {
	t.Helper()
	command := exec.Command(os.Args[0], "-test.run=^"+testName+"$")
	command.Env = proxySubprocessEnv(map[string]string{
		"AGENTPLANE_CLI_PROXY_CHILD":    "1",
		"AGENTPLANE_ACTIONS_URL":        endpoint,
		"AGENTPLANE_ACTIONS_CREDENTIAL": testCredential,
		"HTTP_PROXY":                    httpProxyURL,
		"http_proxy":                    httpProxyURL,
		"HTTPS_PROXY":                   httpsProxyURL,
		"https_proxy":                   httpsProxyURL,
		"ALL_PROXY":                     "",
		"all_proxy":                     "",
		"NO_PROXY":                      "",
		"no_proxy":                      "",
	})
	output, err := command.CombinedOutput()
	if err != nil {
		t.Fatalf("proxy subprocess failed: %v\n%s", err, output)
	}
}

func TestGRPCClientUsesHTTPSProxyCONNECTForH2C(t *testing.T) {
	if os.Getenv("AGENTPLANE_CLI_PROXY_CHILD") == "1" {
		code, stdout, stderr := invoke(actionsEnv(os.Getenv("AGENTPLANE_ACTIONS_URL")), "actions", "groups", "--json")
		if code != 0 || stdout != "" || stderr != "" {
			t.Fatalf("exit %d, stdout %q, stderr %q", code, stdout, stderr)
		}
		return
	}

	fake, directEndpoint := newActionFake(t)
	destination := strings.TrimPrefix(directEndpoint, "http://")
	_, port, err := net.SplitHostPort(destination)
	if err != nil {
		t.Fatal(err)
	}
	target := "actions.test.invalid:" + port
	endpoint := "http://" + target
	connected := make(chan string, 1)
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect {
			t.Errorf("proxy method %s, want CONNECT", r.Method)
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		connected <- r.Host
		client, _, err := w.(http.Hijacker).Hijack()
		if err != nil {
			t.Errorf("hijack proxy connection: %v", err)
			return
		}
		defer client.Close()
		upstream, err := net.Dial("tcp", destination)
		if err != nil {
			t.Errorf("dial local gRPC server: %v", err)
			_, _ = io.WriteString(client, "HTTP/1.1 502 Bad Gateway\r\n\r\n")
			return
		}
		defer upstream.Close()
		if _, err := io.WriteString(client, "HTTP/1.1 200 Connection established\r\n\r\n"); err != nil {
			t.Errorf("write CONNECT response: %v", err)
			return
		}
		go func() { _, _ = io.Copy(upstream, client) }()
		_, _ = io.Copy(client, upstream)
	}))
	defer proxy.Close()
	unusedHTTPProxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		t.Errorf("grpc-go selected HTTP_PROXY for an http Actions URL: %s %s", r.Method, r.URL)
		w.WriteHeader(http.StatusBadGateway)
	}))
	defer unusedHTTPProxy.Close()

	runProxyChildWithProxies(t, "TestGRPCClientUsesHTTPSProxyCONNECTForH2C", endpoint, unusedHTTPProxy.URL, proxy.URL)
	select {
	case got := <-connected:
		if got != target {
			t.Errorf("CONNECT target %q, want %q", got, target)
		}
	default:
		t.Fatal("grpc-go did not open the configured HTTP CONNECT proxy")
	}
	if fake.count("ListActionGroups") != 1 {
		t.Fatalf("gRPC server calls %+v; want ListActionGroups over CONNECT+h2c", fake.callLog())
	}
	for _, call := range fake.callLog() {
		if len(call.authorization) != 1 || call.authorization[0] != "Bearer "+testCredential {
			t.Errorf("authorization metadata %q; want the configured placeholder", call.authorization)
		}
	}
}

func TestGRPCConnectRefusalPreservesProxySourceAndReason(t *testing.T) {
	if os.Getenv("AGENTPLANE_CLI_PROXY_CHILD") == "1" {
		code, stdout, stderr := invoke(actionsEnv(os.Getenv("AGENTPLANE_ACTIONS_URL")), "actions", "groups", "--json")
		got := errorOf(t, stderr)
		if code != exitTransient || stdout != "" || got.Source != sourceEgress || got.Reason == nil || *got.Reason != "no-rule" {
			t.Fatalf("exit %d, stdout %q, error %+v; want the egress CONNECT refusal", code, stdout, got)
		}
		return
	}

	connected := make(chan struct{}, 1)
	proxy := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodConnect {
			t.Errorf("proxy method %s, want CONNECT", r.Method)
		}
		connected <- struct{}{}
		w.Header().Set(egressHeader, "denied; reason=no-rule")
		w.WriteHeader(http.StatusForbidden)
	}))
	defer proxy.Close()
	endpoint := "http://actions.test.invalid:8080"
	runProxyChild(t, "TestGRPCConnectRefusalPreservesProxySourceAndReason", endpoint, proxy.URL)
	select {
	case <-connected:
	default:
		t.Fatal("grpc-go did not send CONNECT through the configured proxy")
	}
}

func TestCredentialIsSentAsBearerAndNeverPrinted(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return nil, status.Error(codes.PermissionDenied, "test refusal")
	}
	for _, args := range [][]string{{"actions", "get", missingID}, {"actions", "get", missingID, "--json"}} {
		code, stdout, stderr := invoke(actionsEnv(endpoint), args...)
		if code != exitRefused || strings.Contains(stdout+stderr, testCredential) {
			t.Errorf("%v: exit %d; the credential appears in the output: %q %q", args, code, stdout, stderr)
		}
	}
	for _, call := range fake.callLog() {
		if len(call.authorization) != 1 || call.authorization[0] != "Bearer "+testCredential {
			t.Errorf("authorization metadata = %q, want bearer placeholder", call.authorization)
		}
	}
}

func TestRedirectIsNotFollowedForTheHTTPClients(t *testing.T) {
	elsewhere := newFake(t, func(w http.ResponseWriter, _ *http.Request, _ seen) { reply(w, 200, `{"subject":{},"policies":[]}`) })
	f := newFake(t, func(w http.ResponseWriter, r *http.Request, _ seen) {
		http.Redirect(w, r, elsewhere.URL, http.StatusFound)
	})
	code, _, _ := invoke(egressEnv(f.URL), "egress", "rules", "--json")
	if code != exitRefused || elsewhere.count() != 0 {
		t.Errorf("exit %d, %d requests followed the redirect; want exit 1 and none", code, elsewhere.count())
	}
}

func TestEachGroupNeedsItsOwnConfiguration(t *testing.T) {
	cases := []struct {
		args    []string
		missing string
	}{
		{[]string{"actions", "groups"}, "AGENTPLANE_ACTIONS_URL"},
		{[]string{"notifications", "subscriptions"}, "AGENTPLANE_NOTIFICATIONS_URL"},
		{[]string{"egress", "rules"}, "AGENTPLANE_EGRESS_URL"},
	}
	for _, tc := range cases {
		code, stdout, stderr := invoke(nil, append(tc.args, "--json")...)
		got := errorOf(t, stderr)
		if code != exitUsage || stdout != "" || !strings.Contains(got.Message, tc.missing) {
			t.Errorf("%v: exit %d, stdout %q, error %+v; want exit 2 naming %s", tc.args, code, stdout, got, tc.missing)
		}
	}
}

func TestUsageErrorsExitBeforeAnyActionRPC(t *testing.T) {
	fake, endpoint := newActionFake(t)
	cases := [][]string{
		{"actions", "get"},
		{"actions", "get", missingID, "--key", "k"},
		{"actions", "show", "no-slash"},
		{"actions", "submit", "g/a"},
		{"actions", "submit", "g/a", "--title", "t", "--arguments", "{not json"},
		{"actions", "submit", "g/a", "--title", "t", "--until", "decision"},
		{"actions", "wait", missingID, "--until", "never"},
		{"actions", "requests", "--limit", "-1"},
		{"actions", "events", missingID, "--after", "2147483648"},
		{"actions", "requests", "--state", "not_a_state"},
	}
	for _, args := range cases {
		code, stdout, stderr := invoke(actionsEnv(endpoint), args...)
		if code != exitUsage || stdout != "" || fake.count("ListActionGroups")+fake.count("GetAction") +
			+fake.count("GetActionPolicy") + fake.count("SubmitActionRequest") + fake.count("GetActionRequest") +
			+fake.count("WatchActionRequest") + fake.count("ListActionRequests") + fake.count("ListActionRequestEvents") != 0 {
			t.Errorf("%v: exit %d, stdout %q, calls %+v, stderr %q; want exit 2 before any action RPC", args, code, stdout, fake.callLog(), stderr)
		}
	}
}

func TestWaitDeadlineUsesTheClientBudget(t *testing.T) {
	fake, endpoint := newActionFake(t)
	fake.getRequest = func(context.Context, *action_servicepb.GetActionRequestRequest) (*action_servicepb.GetActionRequestResponse, error) {
		return &action_servicepb.GetActionRequestResponse{ActionRequest: receiptAt(waitedID, action_servicepb.ActionState_ACTION_STATE_RUNNING)}, nil
	}
	fake.watchRequest = func(_ *action_servicepb.WatchActionRequestRequest, stream grpc.ServerStreamingServer[action_servicepb.WatchActionRequestResponse]) error {
		<-stream.Context().Done()
		return status.FromContextError(stream.Context().Err()).Err()
	}
	code, _, _ := invoke(actionsEnv(endpoint), "actions", "wait", waitedID, "--timeout", "0.2")
	if code != exitDeadline {
		t.Errorf("exit %d; want deadline exit after a 0.2s budget", code)
	}
}
