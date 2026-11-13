package placement_test

import (
	"testing"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/internal/placement"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

func TestPlacementRequestRequiresExactTargetAndNativeHarness(t *testing.T) {
	target := &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "00000000-0000-4000-8000-000000000001"}
	native := func(exact *sessionpb.ExactEnvironment, harness sessionpb.Harness) *dispatcherpb.CreateSessionRequest {
		return &dispatcherpb.CreateSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
			EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: exact, Harness: harness},
		}}}}
	}
	central := func(exact *sessionpb.ExactEnvironment) *dispatcherpb.CreateSessionRequest {
		return &dispatcherpb.CreateSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{
			CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: exact},
		}}}}
	}
	for _, test := range []struct {
		name    string
		request *dispatcherpb.CreateSessionRequest
	}{
		{"missing spec", &dispatcherpb.CreateSessionRequest{}},
		{"missing placement", &dispatcherpb.CreateSessionRequest{Spec: &sessionpb.SessionSpec{}}},
		{"empty placement", &dispatcherpb.CreateSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{}}}},
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
			if _, err := placement.PlacementFromRequest(test.request); status.Code(err) != codes.InvalidArgument {
				t.Fatalf("PlacementFromRequest(%v) = %v, want InvalidArgument", test.request, err)
			}
		})
	}
	for _, test := range []struct {
		name    string
		request *dispatcherpb.CreateSessionRequest
		want    placement.SessionPlacement
	}{
		{"environment hosted", native(target, sessionpb.Harness_HARNESS_CODEX), placement.EnvironmentHosted},
		{"central sampler V1", central(target), placement.CentralSampler},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := placement.PlacementFromRequest(test.request)
			if err != nil || got != test.want {
				t.Fatalf("PlacementFromRequest = %q, %v; want %q", got, err, test.want)
			}
			if !proto.Equal(placement.RequestTarget(test.request), target) {
				t.Fatalf("RequestTarget = %v, want %v", placement.RequestTarget(test.request), target)
			}
		})
	}
}
