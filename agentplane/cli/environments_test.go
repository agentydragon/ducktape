package main

import (
	"context"
	"encoding/json"
	"net"
	"strings"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type environmentFixture struct {
	environmentpb.UnimplementedEnvironmentServiceServer
	resumeRequest *environmentpb.ResumeEnvironmentRequest
	getRequest    *environmentpb.GetEnvironmentRequest
	resumeErr     error
	resumeCalls   int
}

func (f *environmentFixture) GetEnvironment(
	_ context.Context,
	request *environmentpb.GetEnvironmentRequest,
) (*environmentpb.EnvironmentDescriptor, error) {
	f.getRequest = request
	return &environmentpb.EnvironmentDescriptor{
		Target: request.GetTarget(),
		ObservedState: &environmentpb.EnvironmentObservedState{
			Readiness: &environmentpb.Readiness{State: environmentpb.ReadinessState_READINESS_STATE_NOT_READY},
		},
	}, nil
}

func (f *environmentFixture) ResumeEnvironment(_ context.Context, request *environmentpb.ResumeEnvironmentRequest) (*environmentpb.ResumeEnvironmentResponse, error) {
	f.resumeCalls++
	f.resumeRequest = request
	if f.resumeErr != nil {
		return nil, f.resumeErr
	}
	return &environmentpb.ResumeEnvironmentResponse{
		Target:  request.GetTarget(),
		Outcome: environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED,
	}, nil
}

func environmentEndpoint(t *testing.T, fixture *environmentFixture) string {
	t.Helper()
	listener, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer()
	environmentpb.RegisterEnvironmentServiceServer(server, fixture)
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
	fixture := &environmentFixture{}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	if code != 0 || stderr != "" {
		t.Fatalf("resume: exit %d stdout %q stderr %q", code, stdout, stderr)
	}
	if fixture.resumeCalls != 1 || fixture.resumeRequest == nil || fixture.resumeRequest.GetResumeToken() != "opaque-generation-bound-token" ||
		fixture.resumeRequest.GetTarget().GetNamespace() != "agentplane-staging" || fixture.resumeRequest.GetTarget().GetName() != "sandbox-one" ||
		fixture.resumeRequest.GetTarget().GetUid() != "sandbox-uid-1" {
		t.Fatalf("ResumeEnvironment request=%v calls=%d", fixture.resumeRequest, fixture.resumeCalls)
	}
	if !strings.Contains(stdout, `"outcome":"RESUME_OUTCOME_REQUESTED"`) || strings.Contains(stdout, "ready") {
		t.Fatalf("resume response conflated acceptance and readiness: %q", stdout)
	}
}

func TestEnvironmentResumeDoesNotRetryAnUncertainReply(t *testing.T) {
	assessment := &environmentlifecyclepb.WakeAssessment{
		State:  environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
		Reason: environmentlifecyclepb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN,
		Error:  &statuspb.Status{Code: int32(codes.Unavailable), Message: "patch result unknown"},
	}
	resumeStatus, err := status.New(codes.Unavailable, "private backend detail").WithDetails(assessment)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &environmentFixture{resumeErr: resumeStatus.Err()}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	if code != exitExecutionUnknown || stdout != "" || fixture.resumeCalls != 1 || !strings.Contains(stderr, "outcome is unknown") {
		t.Fatalf("uncertain resume: exit %d stdout %q stderr %q calls %d", code, stdout, stderr, fixture.resumeCalls)
	}
	var result struct {
		Error struct {
			WakeAssessment json.RawMessage `json:"wake_assessment"`
		} `json:"error"`
	}
	if err := json.Unmarshal([]byte(stderr), &result); err != nil {
		t.Fatalf("decode uncertain resume error: %v (%q)", err, stderr)
	}
	if !strings.Contains(string(result.Error.WakeAssessment), `"state":"WAKE_STATE_OUTCOME_UNKNOWN"`) ||
		!strings.Contains(string(result.Error.WakeAssessment), `"reason":"WAKE_REASON_OUTCOME_UNKNOWN"`) ||
		!strings.Contains(string(result.Error.WakeAssessment), `"message":"patch result unknown"`) {
		t.Fatalf("uncertain resume dropped wake details: %s", stderr)
	}
}

func TestEnvironmentResumeReportsInvalidDetailsWithoutLosingUnknownOutcome(t *testing.T) {
	assessment := &environmentlifecyclepb.WakeAssessment{
		State:      environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
		ObservedAt: &timestamppb.Timestamp{Seconds: 253402300800},
	}
	resumeStatus, err := status.New(codes.Unavailable, "patch result unknown").WithDetails(assessment)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &environmentFixture{resumeErr: resumeStatus.Err()}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	failure := errorOf(t, stderr)
	if code != exitExecutionUnknown || stdout != "" || fixture.resumeCalls != 1 ||
		!strings.Contains(failure.Message, "outcome is unknown") || !strings.Contains(failure.Message, "encode wake assessment") {
		t.Fatalf("invalid resume detail: exit %d stdout %q stderr %q calls %d", code, stdout, stderr, fixture.resumeCalls)
	}
}

func TestEnvironmentResumePreservesEligibilityDenial(t *testing.T) {
	eligibility := &environmentlifecyclepb.ResumeEligibility{
		State:  environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED,
		Reason: environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED,
	}
	resumeStatus, err := status.New(codes.PermissionDenied, "resume denied").WithDetails(eligibility)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &environmentFixture{resumeErr: resumeStatus.Err()}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	if code != exitRefused || stdout != "" || fixture.resumeCalls != 1 {
		t.Fatalf("denied resume: exit %d stdout %q stderr %q calls %d", code, stdout, stderr, fixture.resumeCalls)
	}
	var result struct {
		Error struct {
			Code              string          `json:"code"`
			ResumeEligibility json.RawMessage `json:"resume_eligibility"`
		} `json:"error"`
	}
	if err := json.Unmarshal([]byte(stderr), &result); err != nil {
		t.Fatalf("decode denied resume error: %v (%q)", err, stderr)
	}
	if result.Error.Code != "PERMISSION_DENIED" ||
		!strings.Contains(string(result.Error.ResumeEligibility), `"state":"RESUME_ELIGIBILITY_STATE_DENIED"`) ||
		!strings.Contains(string(result.Error.ResumeEligibility), `"reason":"RESUME_ELIGIBILITY_REASON_POLICY_DISABLED"`) {
		t.Fatalf("denied resume dropped eligibility details: %s", stderr)
	}
}

func TestEnvironmentResumePreservesRateLimitRetryTime(t *testing.T) {
	retryAt := timestamppb.New(time.Date(2026, time.November, 1, 12, 0, 0, 0, time.UTC))
	assessment := &environmentlifecyclepb.WakeAssessment{
		State:   environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED,
		Reason:  environmentlifecyclepb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED,
		RetryAt: retryAt,
	}
	resumeStatus, err := status.New(codes.ResourceExhausted, "wake budget exhausted").WithDetails(assessment)
	if err != nil {
		t.Fatal(err)
	}
	fixture := &environmentFixture{resumeErr: resumeStatus.Err()}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
	if code != exitTransient || stdout != "" || fixture.resumeCalls != 1 {
		t.Fatalf("rate limited resume: exit %d stdout %q stderr %q calls %d", code, stdout, stderr, fixture.resumeCalls)
	}
	var result struct {
		Error struct {
			WakeAssessment json.RawMessage `json:"wake_assessment"`
		} `json:"error"`
	}
	if err := json.Unmarshal([]byte(stderr), &result); err != nil {
		t.Fatalf("decode rate limited resume error: %v (%q)", err, stderr)
	}
	if !strings.Contains(string(result.Error.WakeAssessment), `"state":"WAKE_STATE_RATE_LIMITED"`) ||
		!strings.Contains(string(result.Error.WakeAssessment), `"reason":"WAKE_REASON_WAKE_BUDGET_EXHAUSTED"`) ||
		!strings.Contains(string(result.Error.WakeAssessment), `"retry_at":"2026-11-01T12:00:00Z"`) {
		t.Fatalf("rate limited resume dropped wake details: %s", stderr)
	}
}

func TestEnvironmentGetReturnsCurrentFacts(t *testing.T) {
	fixture := &environmentFixture{}
	endpoint := environmentEndpoint(t, fixture)
	code, stdout, stderr := invoke(environmentVars(endpoint), "--json", "environments", "get", "agentplane-staging", "sandbox-one", "sandbox-uid-1")
	if code != 0 || stderr != "" || fixture.getRequest == nil {
		t.Fatalf("get: exit %d stdout %q stderr %q request %v", code, stdout, stderr, fixture.getRequest)
	}
	if !strings.Contains(stdout, `"readiness":{"state":"READINESS_STATE_NOT_READY"}`) {
		t.Fatalf("get omitted latest readiness facts : %q", stdout)
	}
}
