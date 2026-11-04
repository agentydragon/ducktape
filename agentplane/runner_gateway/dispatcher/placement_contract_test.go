package dispatcher

import (
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

func TestPlacementRequestRequiresMatchingOwnerAndExactTarget(t *testing.T) {
	native := &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CODEX}
	central := &sessionpb.SessionSpec{Harness: sessionpb.Harness_HARNESS_CENTRAL_SAMPLER}
	target := &dispatcherpb.ExactSandbox{Sandbox: "sandbox", SandboxUid: "00000000-0000-4000-8000-000000000001"}
	environment := func(exact *dispatcherpb.ExactSandbox) *dispatcherpb.OpenSessionRequest {
		return &dispatcherpb.OpenSessionRequest{Spec: native, Placement: &dispatcherpb.OpenSessionRequest_EnvironmentHosted{
			EnvironmentHosted: &dispatcherpb.EnvironmentHostedPlacement{Environment: exact},
		}}
	}
	sampler := func(exact *dispatcherpb.ExactSandbox) *dispatcherpb.OpenSessionRequest {
		return &dispatcherpb.OpenSessionRequest{Spec: central, Placement: &dispatcherpb.OpenSessionRequest_CentralSampler{
			CentralSampler: &dispatcherpb.CentralSamplerPlacement{},
		}, InitialExecutor: exact}
	}
	for _, test := range []struct {
		name    string
		request *dispatcherpb.OpenSessionRequest
	}{
		{"missing placement", &dispatcherpb.OpenSessionRequest{Spec: native}},
		{"missing spec", &dispatcherpb.OpenSessionRequest{Placement: environment(target).Placement}},
		{"native missing environment", environment(nil)},
		{"native empty name", environment(&dispatcherpb.ExactSandbox{SandboxUid: target.SandboxUid})},
		{"native empty UID", environment(&dispatcherpb.ExactSandbox{Sandbox: target.Sandbox})},
		{"native with central executor", func() *dispatcherpb.OpenSessionRequest {
			r := environment(target)
			r.InitialExecutor = target
			return r
		}()},
		{"central missing executor", sampler(nil)},
		{"central empty name", sampler(&dispatcherpb.ExactSandbox{SandboxUid: target.SandboxUid})},
		{"central empty UID", sampler(&dispatcherpb.ExactSandbox{Sandbox: target.Sandbox})},
		{"native variant with central harness", func() *dispatcherpb.OpenSessionRequest { r := environment(target); r.Spec = central; return r }()},
		{"central variant with native harness", func() *dispatcherpb.OpenSessionRequest { r := sampler(target); r.Spec = native; return r }()},
	} {
		t.Run(test.name, func(t *testing.T) {
			if _, err := placementFromRequest(test.request); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("placementFromRequest(%v) = %v, want InvalidArgument", test.request, err)
			}
		})
	}
	for _, test := range []struct {
		name    string
		request *dispatcherpb.OpenSessionRequest
		want    SessionPlacement
	}{
		{"environment hosted", environment(target), EnvironmentHosted},
		{"central sampler", sampler(target), CentralSampler},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := placementFromRequest(test.request)
			if err != nil || got != test.want {
				t.Fatalf("placementFromRequest = %q, %v; want %q", got, err, test.want)
			}
			if requestTarget(test.request) != target {
				t.Fatal("request target changed")
			}
			opening := &dispatcherpb.SessionOpening{}
			if err := got.setWire(opening, target.Sandbox, target.SandboxUid); err != nil {
				t.Fatal(err)
			}
			switch got {
			case EnvironmentHosted:
				if opening.GetEnvironmentHosted().GetEnvironment().GetSandboxUid() != target.SandboxUid || opening.GetInitialExecutor() != nil {
					t.Fatalf("native opening placement = %v", opening)
				}
			case CentralSampler:
				if opening.GetCentralSampler() == nil || opening.GetInitialExecutor().GetSandboxUid() != target.SandboxUid {
					t.Fatalf("central opening placement = %v", opening)
				}
			}
		})
	}
}
