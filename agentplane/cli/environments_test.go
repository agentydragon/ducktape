package cli

import (
	"context"
	"encoding/json"
	"slices"
	"strings"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/cli/internal/invocation"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	statuspb "google.golang.org/genproto/googleapis/rpc/status"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"
)

type environmentFixture struct {
	environmentpb.UnimplementedEnvironmentServiceServer
	resumeRequest *environmentpb.ResumeEnvironmentRequest
	getRequest    *environmentpb.GetEnvironmentRequest
	authorization chan []string
	resumeErr     error
	resumeCalls   int
}

func (f *environmentFixture) GetEnvironment(
	ctx context.Context,
	request *environmentpb.GetEnvironmentRequest,
) (*environmentpb.EnvironmentDescriptor, error) {
	f.getRequest = request
	if f.authorization != nil {
		md, _ := metadata.FromIncomingContext(ctx)
		f.authorization <- md.Get("authorization")
	}
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
	server := grpc.NewServer()
	environmentpb.RegisterEnvironmentServiceServer(server, fixture)
	address := grpcserver.Serve(t, server)
	return "http://" + address
}

func environmentVars(endpoint string) map[string]string {
	return map[string]string{
		"AGENTPLANE_ENVIRONMENTS_URL":        endpoint,
		"AGENTPLANE_ENVIRONMENTS_CREDENTIAL": testCredential,
	}
}

func TestEnvironmentResumeRequiresOpaqueToken(t *testing.T) {
	code, stdout, stderr := invoke(nil, "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1")
	if code != invocation.ExitUsage || stdout != "" || !strings.Contains(stderr, "required flag(s) \"resume-token\"") {
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
	if code != invocation.ExitExecutionUnknown || stdout != "" || fixture.resumeCalls != 1 || !strings.Contains(stderr, "outcome is unknown") {
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
	if code != invocation.ExitExecutionUnknown || stdout != "" || fixture.resumeCalls != 1 ||
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
	if code != invocation.ExitRefused || stdout != "" || fixture.resumeCalls != 1 {
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
	if code != invocation.ExitTransient || stdout != "" || fixture.resumeCalls != 1 {
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

func TestEnvironmentSendsOnlyAConfiguredCredential(t *testing.T) {
	for name, tc := range map[string]struct {
		credential string
		want       []string
	}{
		"a configured credential": {testCredential, []string{"Bearer " + testCredential}},
		"no credential":           {"", nil},
	} {
		t.Run(name, func(t *testing.T) {
			fixture := &environmentFixture{authorization: make(chan []string, 1)}
			env := environmentVars(environmentEndpoint(t, fixture))
			env["AGENTPLANE_ENVIRONMENTS_CREDENTIAL"] = tc.credential

			code, _, stderr := invoke(env, "--json", "environments", "get", "test-ns", "test-sandbox", "test-uid")
			if code != 0 || stderr != "" {
				t.Fatalf("get: exit %d stderr %q", code, stderr)
			}
			if got := <-fixture.authorization; !slices.Equal(got, tc.want) {
				t.Fatalf("EnvironmentService authorization=%v, want %v", got, tc.want)
			}
		})
	}
}

func TestEnvironmentsNeedAUsableEndpointOfTheirOwn(t *testing.T) {
	for name, tc := range map[string]struct{ endpoint, wantStderr string }{
		"no endpoint":         {"", "AGENTPLANE_ENVIRONMENTS_URL not set"},
		"an invalid endpoint": {"not a URL", "AGENTPLANE_ENVIRONMENTS_URL"},
	} {
		t.Run(name, func(t *testing.T) {
			// Nothing listens at the Sessions URL, so a call that reached it would exit 3, not 2.
			env := map[string]string{
				"AGENTPLANE_SESSIONS_URL":        "http://127.0.0.1:1",
				"AGENTPLANE_SESSIONS_CREDENTIAL": testCredential,
				"AGENTPLANE_ENVIRONMENTS_URL":    tc.endpoint,
			}
			code, stdout, stderr := invoke(env, "--json", "environments", "get", "test-ns", "test-sandbox", "test-uid")
			if code != invocation.ExitUsage || stdout != "" || !strings.Contains(stderr, tc.wantStderr) {
				t.Fatalf("exit %d stdout %q stderr %q, want a usage error naming %q", code, stdout, stderr, tc.wantStderr)
			}
		})
	}
}
