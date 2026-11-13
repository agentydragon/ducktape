package dispatcher

import (
	"bytes"
	"testing"
	"time"

	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/internal/placement"
	"google.golang.org/protobuf/proto"
)

func TestDescribeSessionKeepsPlacementAndHidesPrivateInstructions(t *testing.T) {
	target := &sessionpb.ExactEnvironment{Name: "sandbox", Uid: "00000000-0000-4000-8000-000000000001"}
	native := &dispatcherpb.CreateSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_EnvironmentHosted{
		EnvironmentHosted: &sessionpb.EnvironmentHostedPlacement{Environment: target, Harness: sessionpb.Harness_HARNESS_CODEX},
	}}}}
	central := &dispatcherpb.CreateSessionRequest{Spec: &sessionpb.SessionSpec{Placement: &sessionpb.SessionPlacement{Kind: &sessionpb.SessionPlacement_CentralSamplerV1{
		CentralSamplerV1: &sessionpb.CentralSamplerV1Placement{ExecutorEnvironment: target},
	}}}}
	for _, test := range []struct {
		name    string
		request *dispatcherpb.CreateSessionRequest
	}{
		{"environment hosted", native},
		{"central sampler V1", central},
	} {
		t.Run(test.name, func(t *testing.T) {
			got, err := placement.PlacementFromRequest(test.request)
			if err != nil {
				t.Fatal(err)
			}
			test.request.Spec.Instructions = "private opening instructions"
			row := &Session{
				SessionID: target.Uid, SandboxName: target.Name, SandboxUID: target.Uid,
				Placement: got, Spec: test.request.Spec, State: SessionWaitingState, CreatedAt: time.Now(), UpdatedAt: time.Now(),
			}
			opening, err := DescribeSession(row)
			if err != nil {
				t.Fatal(err)
			}
			wire, err := proto.Marshal(opening)
			if err != nil {
				t.Fatal(err)
			}
			if bytes.Contains(wire, []byte(test.request.Spec.Instructions)) {
				t.Fatal("opening read exposed private Session instructions")
			}
			decoded := &dispatcherpb.Session{}
			if err := proto.Unmarshal(wire, decoded); err != nil {
				t.Fatal(err)
			}
			if !proto.Equal(decoded.GetPlacement(), test.request.Spec.GetPlacement()) {
				t.Fatalf("opening placement = %v, want shared placement %v", decoded.GetPlacement(), test.request.Spec.GetPlacement())
			}
			switch got {
			case placement.EnvironmentHosted:
				if !proto.Equal(decoded.GetPlacement().GetEnvironmentHosted(), test.request.Spec.GetPlacement().GetEnvironmentHosted()) || decoded.GetPlacement().GetCentralSamplerV1() != nil {
					t.Fatalf("native opening placement = %v", decoded)
				}
			case placement.CentralSampler:
				if !proto.Equal(decoded.GetPlacement().GetCentralSamplerV1(), test.request.Spec.GetPlacement().GetCentralSamplerV1()) || decoded.GetPlacement().GetEnvironmentHosted() != nil {
					t.Fatalf("central opening placement = %v", decoded)
				}
			}
		})
	}
}
