package dispatcher

import (
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/session_dispatcherpb"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type SessionPlacement string

const (
	EnvironmentHosted SessionPlacement = "environment_hosted"
	CentralSampler    SessionPlacement = "central_sampler"
)

func placementFromRequest(request *dispatcherpb.OpenSessionRequest) (SessionPlacement, error) {
	if request.GetSpec() == nil {
		return "", status.Error(codes.InvalidArgument, "Session spec is required")
	}
	switch request.GetPlacement().(type) {
	case *dispatcherpb.OpenSessionRequest_EnvironmentHosted:
		if request.GetInitialExecutor() != nil || !validExactSandbox(request.GetEnvironmentHosted().GetEnvironment()) {
			return "", status.Error(codes.InvalidArgument, "environment_hosted requires one exact environment and no initial_executor")
		}
		if request.GetSpec().GetHarness() == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
			return "", status.Error(codes.InvalidArgument, "central sampler harness requires central_sampler placement")
		}
		return EnvironmentHosted, nil
	case *dispatcherpb.OpenSessionRequest_CentralSampler:
		if !validExactSandbox(request.GetInitialExecutor()) {
			return "", status.Error(codes.InvalidArgument, "central_sampler currently requires an exact initial_executor")
		}
		if request.GetSpec().GetHarness() != sessionpb.Harness_HARNESS_CENTRAL_SAMPLER {
			return "", status.Error(codes.InvalidArgument, "central_sampler placement requires central sampler harness")
		}
		return CentralSampler, nil
	default:
		return "", status.Error(codes.InvalidArgument, "Session placement is required")
	}
}

func validExactSandbox(target *dispatcherpb.ExactSandbox) bool {
	return target != nil && target.GetSandbox() != "" && target.GetSandboxUid() != ""
}

func requestTarget(request *dispatcherpb.OpenSessionRequest) *dispatcherpb.ExactSandbox {
	if request.GetEnvironmentHosted() != nil {
		return request.GetEnvironmentHosted().GetEnvironment()
	}
	return request.GetInitialExecutor()
}

func (p SessionPlacement) setWire(opening *dispatcherpb.SessionOpening, sandboxName, sandboxUID string) error {
	target := &dispatcherpb.ExactSandbox{Sandbox: sandboxName, SandboxUid: sandboxUID}
	switch p {
	case EnvironmentHosted:
		opening.Placement = &dispatcherpb.SessionOpening_EnvironmentHosted{EnvironmentHosted: &dispatcherpb.EnvironmentHostedPlacement{Environment: target}}
	case CentralSampler:
		opening.Placement = &dispatcherpb.SessionOpening_CentralSampler{CentralSampler: &dispatcherpb.CentralSamplerPlacement{}}
		opening.InitialExecutor = target
	default:
		return status.Error(codes.Internal, "unknown stored Session placement")
	}
	return nil
}

func (p SessionPlacement) requiresReadyExecutor() bool { return p == EnvironmentHosted }

func (p SessionPlacement) matchesHarness(spec *sessionpb.SessionSpec) bool {
	if spec == nil {
		return false
	}
	switch p {
	case EnvironmentHosted:
		return spec.GetHarness() != sessionpb.Harness_HARNESS_CENTRAL_SAMPLER
	case CentralSampler:
		return spec.GetHarness() == sessionpb.Harness_HARNESS_CENTRAL_SAMPLER
	default:
		return false
	}
}
