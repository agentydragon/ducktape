// Package resume implements EnvironmentService explicit resume without an automatic-wake budget.
package resume

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/environments/lifecycle"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// Manager owns explicit-resume RPC handling; automatic wake execution stays in Dispatcher.
type Manager struct{ policy *lifecycle.Policy }

func NewManager(options lifecycle.Options) (*Manager, error) {
	policy, err := lifecycle.NewPolicy(options)
	if err != nil {
		return nil, err
	}
	return &Manager{policy: policy}, nil
}

func (s *Manager) Eligibility(ctx context.Context, target *lifecycle.Target) (*environmentlifecyclepb.ResumeEligibility, int64, error) {
	return s.policy.Eligibility(ctx, target)
}

func (s *Manager) AssessAutomaticWake(ctx context.Context, target *lifecycle.Target, caller lifecycle.WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	return s.policy.AssessAutomaticWake(ctx, target, caller)
}

func (s *Manager) ResumeEnvironment(
	ctx context.Context,
	request *environmentpb.ResumeEnvironmentRequest,
) (*environmentpb.ResumeEnvironmentResponse, error) {
	if request == nil || request.Target == nil {
		return nil, status.Error(codes.InvalidArgument, "target is required")
	}
	target := targetFromProto(request.Target)
	if !lifecycle.ValidTarget(target) {
		return nil, status.Error(codes.InvalidArgument, "target namespace, name and UID are required")
	}
	caller, operator, err := s.policy.AdmittedResumeCaller(ctx)
	if err != nil {
		return nil, err
	}
	view, err := s.policy.ReadTarget(ctx, target)
	if err != nil {
		return nil, err
	}
	eligibility, err := s.policy.EligibilityFor(ctx, view, caller, operator)
	if err != nil {
		return nil, err
	}
	if eligibility.GetState() != environmentlifecyclepb.ResumeEligibilityState_RESUME_ELIGIBILITY_STATE_AVAILABLE {
		return nil, s.policy.ResumeDenial(ctx, target, caller, eligibility)
	}
	if request.GetResumeToken() == "" && !operator {
		return nil, status.Error(codes.InvalidArgument, "resume_token is required")
	}
	if token := request.GetResumeToken(); token != "" && token != lifecycle.NewResumeToken(target, view.Spec.WakeGeneration) {
		return stateChangedResponse(target, view, eligibility, environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED), nil
	}

	resumeToken := request.GetResumeToken()
	if resumeToken == "" {
		// A direct operator may start a fresh intent without a Discovery token. Capture the
		// observed generation anyway so conflict retries cannot silently advance a later one.
		resumeToken = lifecycle.NewResumeToken(target, view.Spec.WakeGeneration)
	}
	assessment := s.policy.ApplyExplicitResume(ctx, target, caller, resumeToken, operator)
	switch assessment.GetState() {
	case environmentlifecyclepb.WakeState_WAKE_STATE_REQUESTED:
		return resumeResponse(target, assessment.GetTargetGeneration(), environmentpb.ResumeOutcome_RESUME_OUTCOME_REQUESTED, eligibility, assessment), nil
	case environmentlifecyclepb.WakeState_WAKE_STATE_NOT_NEEDED:
		return resumeResponse(target, assessment.GetTargetGeneration(), environmentpb.ResumeOutcome_RESUME_OUTCOME_ALREADY_RUNNING, eligibility, assessment), nil
	case environmentlifecyclepb.WakeState_WAKE_STATE_DENIED:
		current, err := s.policy.ReadTarget(ctx, target)
		if err != nil {
			return nil, err
		}
		currentEligibility, err := s.policy.EligibilityFor(ctx, current, caller, operator)
		if err != nil {
			return nil, err
		}
		return nil, s.policy.ResumeDenial(ctx, target, caller, currentEligibility)
	case environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED:
		if assessment.GetReason() != environmentlifecyclepb.WakeReason_WAKE_REASON_TARGET_CHANGED {
			return nil, status.Error(codes.Unavailable, "resume state is unavailable")
		}
		current, err := s.policy.ReadTarget(ctx, target)
		if err != nil {
			return nil, err
		}
		currentEligibility, err := s.policy.EligibilityFor(ctx, current, caller, operator)
		if err != nil {
			return nil, err
		}
		return stateChangedResponse(target, current, currentEligibility, assessment.GetReason()), nil
	case environmentlifecyclepb.WakeState_WAKE_STATE_OUTCOME_UNKNOWN:
		return nil, rpcWithAssessment(codes.Unavailable, "resume outcome is unknown", assessment)
	case environmentlifecyclepb.WakeState_WAKE_STATE_FAILED:
		return nil, assessmentRPCError(assessment)
	default:
		return nil, status.Error(codes.Unavailable, "resume state is unavailable")
	}
}

func rpcWithAssessment(code codes.Code, message string, assessment *environmentlifecyclepb.WakeAssessment) error {
	result := status.New(code, message)
	withDetails, err := result.WithDetails(assessment)
	if err != nil {
		return result.Err()
	}
	return withDetails.Err()
}

func assessmentRPCError(assessment *environmentlifecyclepb.WakeAssessment) error {
	code := codes.Unavailable
	message := "Sandbox state is unavailable"
	if assessment.GetError() != nil {
		code = codes.Code(assessment.GetError().GetCode())
		message = assessment.GetError().GetMessage()
	}
	return rpcWithAssessment(code, message, assessment)
}

func resumeResponse(
	target *lifecycle.Target,
	generation int64,
	outcome environmentpb.ResumeOutcome,
	eligibility *environmentlifecyclepb.ResumeEligibility,
	wake *environmentlifecyclepb.WakeAssessment,
) *environmentpb.ResumeEnvironmentResponse {
	return &environmentpb.ResumeEnvironmentResponse{
		Target: targetProto(target), Outcome: outcome, WakeGeneration: proto.Int64(generation), Eligibility: eligibility, Wake: wake,
	}
}

func stateChangedResponse(
	target *lifecycle.Target,
	view *sandboxapi.Sandbox,
	eligibility *environmentlifecyclepb.ResumeEligibility,
	reason environmentlifecyclepb.WakeReason,
) *environmentpb.ResumeEnvironmentResponse {
	wake := lifecycle.AssessmentFor(environmentlifecyclepb.WakeState_WAKE_STATE_NOT_EVALUATED, reason, view.Spec.WakeGeneration)
	return resumeResponse(target, view.Spec.WakeGeneration, environmentpb.ResumeOutcome_RESUME_OUTCOME_STATE_CHANGED, eligibility, wake)
}

func targetFromProto(target *identitypb.EnvironmentTarget) *lifecycle.Target {
	if target == nil {
		return nil
	}
	return target
}

func targetProto(target *lifecycle.Target) *identitypb.EnvironmentTarget {
	return target
}
