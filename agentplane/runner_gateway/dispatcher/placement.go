package dispatcher

import (
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	dispatcherpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/session/session_dispatcherpb"
	"github.com/google/uuid"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

type SessionPlacement string

const (
	EnvironmentHosted SessionPlacement = "environment_hosted"
	CentralSampler    SessionPlacement = "central_sampler_v1"
)

func placementOf(spec *sessionpb.SessionSpec) SessionPlacement {
	placement := spec.GetPlacement()
	if placement == nil {
		return ""
	}
	switch placement.Kind.(type) {
	case *sessionpb.SessionPlacement_EnvironmentHosted:
		return EnvironmentHosted
	case *sessionpb.SessionPlacement_CentralSamplerV1:
		return CentralSampler
	default:
		return ""
	}
}

func placementFromRequest(request *dispatcherpb.CreateSessionRequest) (SessionPlacement, error) {
	if request.GetSpec() == nil {
		return "", status.Error(codes.InvalidArgument, "Session spec is required")
	}
	spec := request.GetSpec()
	switch placementOf(spec) {
	case EnvironmentHosted:
		if !validExactEnvironment(spec.GetPlacement().GetEnvironmentHosted().GetEnvironment()) ||
			(spec.GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CLAUDE &&
				spec.GetPlacement().GetEnvironmentHosted().GetHarness() != sessionpb.Harness_HARNESS_CODEX) {
			return "", status.Error(codes.InvalidArgument, "environment_hosted requires one exact environment and a native harness")
		}
		return EnvironmentHosted, nil
	case CentralSampler:
		if !validExactEnvironment(spec.GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()) {
			return "", status.Error(codes.InvalidArgument, "central_sampler_v1 currently requires one exact executor environment")
		}
		return CentralSampler, nil
	default:
		return "", status.Error(codes.InvalidArgument, "Session placement is required")
	}
}

func validExactEnvironment(target *sessionpb.ExactEnvironment) bool {
	if target == nil || target.GetName() == "" {
		return false
	}
	_, err := uuid.Parse(target.GetUid())
	return err == nil
}

func requestTarget(request *dispatcherpb.CreateSessionRequest) *sessionpb.ExactEnvironment {
	if request.GetSpec().GetPlacement().GetEnvironmentHosted() != nil {
		return request.GetSpec().GetPlacement().GetEnvironmentHosted().GetEnvironment()
	}
	return request.GetSpec().GetPlacement().GetCentralSamplerV1().GetExecutorEnvironment()
}

func (p SessionPlacement) requiresReadyExecutor() bool { return p == EnvironmentHosted }

func (p SessionPlacement) matchesSpec(spec *sessionpb.SessionSpec) bool {
	return p != "" && placementOf(spec) == p
}
