package dispatcher

import (
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestPlacementRequestRequiresExactTargetAndNativeHarness(t *testing.T) {
	target := &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "00000000-0000-4000-8000-000000000001"}
	native := func(exact *sessionpb.ExactEnvironment, harness sessionpb.Harness) *dispatcherpb.OpenSessionRequest {
		return &dispatcherpb.OpenSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionSpec_EnvironmentHosted{
			EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: exact, Harness: harness},
		}}}
	}
	central := func(exact *sessionpb.ExactEnvironment) *dispatcherpb.OpenSessionRequest {
		return &dispatcherpb.OpenSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionSpec_CentralSamplerV1{
			CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: exact},
		}}}
	}
	for _, test := range []struct {
		name    string
		request *dispatcherpb.OpenSessionRequest
	}{
		{"missing spec", &dispatcherpb.OpenSessionRequest{}},
		{"missing placement", &dispatcherpb.OpenSessionRequest{Spec: &sessionpb.SessionSpec{}}},
		{"native missing environment", native(nil, sessionpb.Harness_HARNESS_CODEX)},
		{"native empty name", native(&sessionpb.ExactEnvironment{Uid: target.Uid}, sessionpb.Harness_HARNESS_CODEX)},
		{"native empty UID", native(&sessionpb.ExactEnvironment{Name: target.Name}, sessionpb.Harness_HARNESS_CODEX)},
		{"native invalid UID", native(&sessionpb.ExactEnvironment{Name: target.Name, Uid: "not-a-uuid"}, sessionpb.Harness_HARNESS_CODEX)},
		{"native missing harness", native(target, sessionpb.Harness_HARNESS_UNSPECIFIED)},
		{"native central harness", native(target, sessionpb.Harness_HARNESS_CENTRAL_SAMPLER)},
		{"central missing executor", central(nil)},
		{"central empty name", central(&sessionpb.ExactEnvironment{Uid: target.Uid})},
		{"central empty UID", central(&sessionpb.ExactEnvironment{Name: target.Name})},
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
		{"environment hosted", native(target, sessionpb.Harness_HARNESS_CODEX), EnvironmentHosted},
		{"central sampler V1", central(target), CentralSampler},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := placementFromRequest(test.request)
			if err != nil || got != test.want {
				t.Fatalf("placementFromRequest = %q, %v; want %q", got, err, test.want)
			}
			if !proto.Equal(requestTarget(test.request), target) {
				t.Fatalf("request target = %v, want %v", requestTarget(test.request), target)
			}
			row := &Opening{
				SessionID: target.Uid, SandboxName: target.Name, SandboxUID: target.Uid,
				Placement: got, Spec: test.request.Spec, State: QueueState, CreatedAt: time.Now(), UpdatedAt: time.Now(),
			}
			opening, err := DescribeOpening(row)
			if err != nil {
				t.Fatal(err)
			}
			wire, err := proto.Marshal(opening)
			if err != nil {
				t.Fatal(err)
			}
			decoded := &dispatcherpb.SessionOpening{}
			if err := proto.Unmarshal(wire, decoded); err != nil {
				t.Fatal(err)
			}
			switch got {
			case EnvironmentHosted:
				if !proto.Equal(decoded.GetEnvironmentHosted(), test.request.Spec.GetEnvironmentHosted()) || decoded.GetCentralSamplerV1() != nil {
					t.Fatalf("native opening placement = %v", decoded)
				}
			case CentralSampler:
				if !proto.Equal(decoded.GetCentralSamplerV1(), test.request.Spec.GetCentralSamplerV1()) || decoded.GetEnvironmentHosted() != nil {
					t.Fatalf("central opening placement = %v", decoded)
				}
			}
		})
	}
}
