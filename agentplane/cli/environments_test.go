package cli

import (
	"context"
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
	"google.golang.org/protobuf/protoadapt"
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

func TestEnvironmentResumeFailuresKeepTheBackendsDetails(t *testing.T) {
	for _, tc := range []struct {
		name            string
		code            codes.Code
		message         string
		detail          protoadapt.MessageV1
		wantExit        int
		wantCode        string
		wantMessage     []string
		wantWake        []string
		wantEligibility []string
	}{
		{
			name: "an uncertain reply is reported as unknown and not retried", code: codes.Unavailable, message: "private backend detail",
			detail: &environmentlifecyclepb.WakeAssessment{
				State:  environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
				Reason: environmentlifecyclepb.WakeReason_WAKE_REASON_OUTCOME_UNKNOWN,
				Error:  &statuspb.Status{Code: int32(codes.Unavailable), Message: "patch result unknown"},
			},
			wantExit:    invocation.ExitExecutionUnknown,
			wantMessage: []string{"outcome is unknown"},
			wantWake:    []string{`"state":"WAKE_STATE_OUTCOME_UNKNOWN"`, `"reason":"WAKE_REASON_OUTCOME_UNKNOWN"`, `"message":"patch result unknown"`},
		},
		{
			name: "details that cannot be encoded do not hide an unknown outcome", code: codes.Unavailable, message: "patch result unknown",
			detail: &environmentlifecyclepb.WakeAssessment{
				State:      environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN,
				ObservedAt: &timestamppb.Timestamp{Seconds: 253402300800},
			},
			wantExit:    invocation.ExitExecutionUnknown,
			wantMessage: []string{"outcome is unknown", "encode wake assessment"},
		},
		{
			name: "an eligibility denial keeps its reason", code: codes.PermissionDenied, message: "resume denied",
			detail: &environmentlifecyclepb.ResumeEligibility{
				State:  environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_DENIED,
				Reason: environmentlifecyclepb.ResumeEligibilityReason_RESUME_ELIGIBILITY_REASON_POLICY_DISABLED,
			},
			wantExit:        invocation.ExitRefused,
			wantCode:        "PERMISSION_DENIED",
			wantEligibility: []string{`"state":"RESUME_ELIGIBILITY_STATE_DENIED"`, `"reason":"RESUME_ELIGIBILITY_REASON_POLICY_DISABLED"`},
		},
		{
			name: "a rate limit keeps its retry time", code: codes.ResourceExhausted, message: "wake budget exhausted",
			detail: &environmentlifecyclepb.WakeAssessment{
				State:   environmentlifecyclepb.WakeState_WAKE_STATE_RATE_LIMITED,
				Reason:  environmentlifecyclepb.WakeReason_WAKE_REASON_WAKE_BUDGET_EXHAUSTED,
				RetryAt: timestamppb.New(time.Date(2026, time.November, 1, 12, 0, 0, 0, time.UTC)),
			},
			wantExit: invocation.ExitTransient,
			wantWake: []string{`"state":"WAKE_STATE_RATE_LIMITED"`, `"reason":"WAKE_REASON_WAKE_BUDGET_EXHAUSTED"`, `"retry_at":"2026-11-01T12:00:00Z"`},
		},
	} {
		t.Run(tc.name, func(t *testing.T) {
			resumeStatus, err := status.New(tc.code, tc.message).WithDetails(tc.detail)
			if err != nil {
				t.Fatal(err)
			}
			fixture := &environmentFixture{resumeErr: resumeStatus.Err()}
			code, stdout, stderr := invoke(environmentVars(environmentEndpoint(t, fixture)), "--json", "environments", "resume", "agentplane-staging", "sandbox-one", "sandbox-uid-1", "--resume-token", "opaque-generation-bound-token")
			if code != tc.wantExit || stdout != "" || fixture.resumeCalls != 1 {
				t.Fatalf("exit %d stdout %q stderr %q calls %d, want exit %d after one call", code, stdout, stderr, fixture.resumeCalls, tc.wantExit)
			}
			failure := errorOf(t, stderr)
			if tc.wantCode != "" && failure.Code != tc.wantCode {
				t.Errorf("error code %q, want %q", failure.Code, tc.wantCode)
			}
			for _, field := range []struct {
				name string
				got  string
				want []string
			}{
				{"message", failure.Message, tc.wantMessage},
				{"wake_assessment", string(failure.WakeAssessment), tc.wantWake},
				{"resume_eligibility", string(failure.ResumeEligibility), tc.wantEligibility},
			} {
				for _, want := range field.want {
					if !strings.Contains(field.got, want) {
						t.Errorf("error %s = %q, want it to contain %s", field.name, field.got, want)
					}
				}
			}
		})
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
