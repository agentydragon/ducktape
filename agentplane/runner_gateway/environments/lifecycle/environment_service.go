package lifecycle

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/environments/access"
	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/policy/reader"
	environmentlifecyclepb "github.com/agentydragon/ducktape/agentplane/proto/environmentlifecyclepb"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// EnvironmentServiceLifecycle is the subset of lifecycle operations used by EnvironmentService.
// Automatic wake assessment is read-only; automatic wake requests remain available only through
// Manager, whose constructor requires a shared wake budget.
type EnvironmentServiceLifecycle interface {
	Eligibility(context.Context, *Target) (*environmentlifecyclepb.ResumeEligibility, int64, error)
	AssessAutomaticWake(context.Context, *Target, WakeCaller) (*environmentlifecyclepb.WakeAssessment, error)
	ResumeEnvironment(context.Context, *environmentpb.ResumeEnvironmentRequest) (*environmentpb.ResumeEnvironmentResponse, error)
}

// EnvironmentServiceOptions configure policy and explicit resume for EnvironmentService. This
// process assesses automatic-wake eligibility for inventory responses but never reserves budget
// or issues automatic wake requests.
type EnvironmentServiceOptions struct {
	Namespace              string
	Objects                client.Client
	WakeCallers            []auth.Account
	WakeServiceCallers     []string
	OperatorServiceCallers []string
	OperatorCallers        []auth.Account
	EnvironmentRules       *reader.Index[access.Rule]
	PolicyReady            func() error
}

// EnvironmentServiceManager provides EnvironmentService's policy and explicit-resume operations
// without constructing automatic-wake execution capability.
type EnvironmentServiceManager struct {
	manager *Manager
}

func NewEnvironmentServiceManager(options EnvironmentServiceOptions) (*EnvironmentServiceManager, error) {
	manager, err := newManager(Options{
		Namespace:              options.Namespace,
		Objects:                options.Objects,
		WakeCallers:            options.WakeCallers,
		WakeServiceCallers:     options.WakeServiceCallers,
		OperatorServiceCallers: options.OperatorServiceCallers,
		OperatorCallers:        options.OperatorCallers,
		EnvironmentRules:       options.EnvironmentRules,
		PolicyReady:            options.PolicyReady,
	})
	if err != nil {
		return nil, err
	}
	return &EnvironmentServiceManager{manager: manager}, nil
}

func (s *EnvironmentServiceManager) Eligibility(ctx context.Context, target *Target) (*environmentlifecyclepb.ResumeEligibility, int64, error) {
	return s.manager.Eligibility(ctx, target)
}

func (s *EnvironmentServiceManager) AssessAutomaticWake(ctx context.Context, target *Target, caller WakeCaller) (*environmentlifecyclepb.WakeAssessment, error) {
	return s.manager.AssessAutomaticWake(ctx, target, caller)
}

func (s *EnvironmentServiceManager) ResumeEnvironment(ctx context.Context, request *environmentpb.ResumeEnvironmentRequest) (*environmentpb.ResumeEnvironmentResponse, error) {
	return s.manager.ResumeEnvironment(ctx, request)
}

var (
	_ EnvironmentServiceLifecycle = (*Manager)(nil)
	_ EnvironmentServiceLifecycle = (*EnvironmentServiceManager)(nil)
)
