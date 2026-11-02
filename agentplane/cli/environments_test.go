package main

import (
	"context"
	"net"
	"strings"
	"testing"

	discoverypb "github.com/agentydragon/ducktape/agentplane/sandboxes/discovery/discoverypb"
	lifecyclepb "github.com/agentydragon/ducktape/agentplane/sandboxes/lifecycle/lifecyclepb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type environmentLifecycleFixture struct {
	lifecyclepb.UnimplementedEnvironmentLifecycleServer
	request *lifecyclepb.ResumeEnvironmentRequest
	err     error
	calls   int
}

type environmentDiscoveryFixture struct {
	discoverypb.UnimplementedEnvironmentDiscoveryServer
	request *discoverypb.GetEnvironmentRequest
}

func (f *environmentDiscoveryFixture) GetEnvironment(
	_ context.Context,
	request *discoverypb.GetEnvironmentRequest,
) (*discoverypb.EnvironmentDescriptor, error) {
	f.request = request
	return &discoverypb.EnvironmentDescriptor{
		Target:    request.GetTarget(),
		Readiness: &discoverypb.Readiness{State: discoverypb.ReadinessState_READINESS_STATE_NOT_READY},
	}, nil
}

func (f *environmentLifecycleFixture) ResumeEnvironment(_ context.Context, request *lifecyclepb.ResumeEnvironmentRequest) (*lifecyclepb.ResumeEnvironmentResponse, error) {
	f.calls++
	f.request = request
	if f.err != nil {
		return nil, f.err
	}
	return &lifecyclepb.ResumeEnvironmentResponse{
		Target:  request.GetTarget(),
		Outcome: lifecyclepb.ResumeOutcome_RESUME_OUTCOME_REQUESTED,
	}, nil
}

func environmentEndpoint(t *testing.T, fixture *environmentLifecycleFixture) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	lifecyclepb.RegisterEnvironmentLifecycleServer(server, fixture)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return "http://" + listener.Addr().String()
}

func environmentVars(endpoint string) map[string]string {
	return map[string]string{
		"AGENTPLANE_SESSIONS_URL":        endpoint,
		"AGENTPLANE_SESSIONS_CREDENTIAL": testCredential,
	}
}

func TestEnvironmentResumeRequiresOpaqueToken(t *testing.T) {
	code, stdout, stderr := invoke(nil, "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "required flag(s) \"resume-token\"") {
		t.Fatalf("resume without token: exit %d stdout %q stderr %q", code, stdout, stderr)
	}
}

func TestEnvironmentResumeUsesExactTargetAndToken(t *testing.T) {
	fixture := &environmentLifecycleFixture{}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	if code != 0 || stderr != "" {
		t.Fatalf("resume: exit %d stdout %q stderr %q", code, stdout, stderr)
	}
	if fixture.calls != 1 || fixture.request == nil || fixture.request.GetResumeToken() != "opaque-generation-bound-token" ||
		fixture.request.GetTarget().GetNamespace() != "agentplane-staging" || fixture.request.GetTarget().GetName() != "sandbox-one" ||
		fixture.request.GetTarget().GetUid() != "sandbox-uid-1" {
		t.Fatalf("ResumeEnvironment request=%v calls=%d", fixture.request, fixture.calls)
	}
	if !strings.Contains(stdout, `"outcome":"RESUME_OUTCOME_REQUESTED"`) || strings.Contains(stdout, "ready") {
		t.Fatalf("resume response conflated acceptance and readiness: %q", stdout)
	}
}

func TestEnvironmentResumeDoesNotRetryAnUncertainReply(t *testing.T) {
	fixture := &environmentLifecycleFixture{err: status.Error(codes.Unavailable, "private backend detail")}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	if code != exitExecutionUnknown || stdout != "" || fixture.calls != 1 || !strings.Contains(stderr, "outcome is unknown") {
		t.Fatalf("uncertain resume: exit %d stdout %q stderr %q calls %d", code, stdout, stderr, fixture.calls)
	}
}

func TestEnvironmentGetCanBoundReadinessWaitAndReturnsCurrentFacts(t *testing.T) {
	lifecycle := &environmentLifecycleFixture{}
	discovery := &environmentDiscoveryFixture{}
	endpoint := environmentEndpointWithDiscovery(t, lifecycle, discovery)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "get", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--wait-seconds", "8")
	if code != 0 || stderr != "" || discovery.request == nil || discovery.request.GetWaitForReadySeconds() != 8 {
		t.Fatalf("get: exit %d stdout %q stderr %q request %v", code, stdout, stderr, discovery.request)
	}
	if !strings.Contains(stdout, `"readiness":{"state":"READINESS_STATE_NOT_READY"}`) {
		t.Fatalf("get omitted latest readiness facts after bounded wait: %q", stdout)
	}
}

func TestEnvironmentGetRejectsUnboundedWaitBeforeConnecting(t *testing.T) {
	code, stdout, stderr := invoke(nil, "environments", "get", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--wait-seconds", "31")
	if code != exitUsage || stdout != "" || !strings.Contains(stderr, "between 0 and 30") {
		t.Fatalf("get invalid wait: exit %d stdout %q stderr %q", code, stdout, stderr)
	}
}

func environmentEndpointWithDiscovery(
	t *testing.T,
	lifecycle *environmentLifecycleFixture,
	discovery *environmentDiscoveryFixture,
) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	lifecyclepb.RegisterEnvironmentLifecycleServer(server, lifecycle)
	discoverypb.RegisterEnvironmentDiscoveryServer(server, discovery)
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
	})
	return "http://" + listener.Addr().String()
}
