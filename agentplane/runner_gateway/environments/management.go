package environments

import (
	"context"
	"errors"
	"log/slog"

	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/policy"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/internal/management"
	"github.com/agentydragon/ducktape/agentplane/workload_auth/auth"
	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
)

// manager requires an explicit direct, Pod-bound grant. In particular, delegation and read access
// do not confer write authority. Create records the authenticated account as the original caller.
func (s *Service) manager(ctx context.Context) (*management.Access, error) {
	return s.managementAccess(ctx, nil)
}

func (s *Service) managementAccess(ctx context.Context, origin *environmentpb.EnvironmentLaunchOrigin) (*management.Access, error) {
	identity, err := caller(ctx)
	if err != nil {
		return nil, err
	}
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	direct, ok := admission.(auth.DirectAdmission)
	if !ok || direct.Pod == nil || !direct.Pod.IsPodBound() {
		return nil, status.Error(codes.PermissionDenied, "environment management requires a direct Pod-bound caller")
	}
	account := serviceAccountReference(identity)
	if origin == nil {
		if _, ok := s.managers[account]; !ok {
			return nil, status.Error(codes.PermissionDenied, "caller cannot manage environments")
		}
	} else {
		if _, ok := s.launchers[account]; !ok {
			return nil, status.Error(codes.PermissionDenied, "caller cannot launch environments on behalf of an Action caller")
		}
		if origin.GetCaller().GetNamespace() == "" || origin.GetCaller().GetName() == "" || origin.GetActionGroup() == "" {
			return nil, status.Error(codes.InvalidArgument, "launch origin requires caller and Action group")
		}
	}
	return management.New(s.namespace, s.objects, s.defaultPolicySets, &identitypb.ServiceAccount{
		Namespace: account.Namespace, Name: account.Name,
	})
}

func (s *Service) mutationTarget(target *identitypb.EnvironmentTarget) (string, string, error) {
	if target == nil || target.GetNamespace() == "" || target.GetName() == "" || target.GetUid() == "" {
		return "", "", status.Error(codes.InvalidArgument, "complete target handle is required")
	}
	if target.GetNamespace() != s.namespace {
		return "", "", status.Error(codes.NotFound, "environment not found")
	}
	return target.GetName(), target.GetUid(), nil
}

func (s *Service) ListEnvironmentTemplates(ctx context.Context, _ *emptypb.Empty) (*environmentpb.ListEnvironmentTemplatesResponse, error) {
	access, err := s.manager(ctx)
	if err != nil {
		return nil, err
	}
	names, err := access.ListTemplates(ctx)
	if err != nil {
		return nil, managementError(ctx, err)
	}
	return &environmentpb.ListEnvironmentTemplatesResponse{Names: names}, nil
}

func (s *Service) CreateEnvironment(ctx context.Context, request *environmentpb.CreateEnvironmentRequest) (*identitypb.EnvironmentTarget, error) {
	access, err := s.managementAccess(ctx, request.GetLaunchOrigin())
	if err != nil {
		return nil, err
	}
	created, err := access.Create(ctx, request)
	if err != nil {
		return nil, managementError(ctx, err)
	}
	if created.GetNamespace() != s.namespace || created.GetName() != request.GetName() || created.GetUid() == "" {
		return nil, status.Error(codes.Unavailable, "created environment identity is incomplete")
	}
	return created, nil
}

func (s *Service) SuspendEnvironment(ctx context.Context, request *environmentpb.EnvironmentMutationRequest) (*emptypb.Empty, error) {
	access, err := s.manager(ctx)
	if err != nil {
		return nil, err
	}
	name, uid, err := s.mutationTarget(request.GetTarget())
	if err != nil {
		return nil, err
	}
	if err := access.Suspend(ctx, name, uid); err != nil {
		return nil, managementError(ctx, err)
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) DeleteEnvironment(ctx context.Context, request *environmentpb.EnvironmentMutationRequest) (*emptypb.Empty, error) {
	access, err := s.manager(ctx)
	if err != nil {
		return nil, err
	}
	name, uid, err := s.mutationTarget(request.GetTarget())
	if err != nil {
		return nil, err
	}
	if err := access.Delete(ctx, name, uid); err != nil {
		return nil, managementError(ctx, err)
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) SetEnvironmentWakeOnSend(ctx context.Context, request *environmentpb.SetEnvironmentWakeOnSendRequest) (*emptypb.Empty, error) {
	access, err := s.manager(ctx)
	if err != nil {
		return nil, err
	}
	name, uid, err := s.mutationTarget(request.GetTarget())
	if err != nil {
		return nil, err
	}
	if err := access.SetWakeOnSend(ctx, name, uid, request.GetWakeOnSend()); err != nil {
		return nil, managementError(ctx, err)
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) SetEnvironmentIdleSuspend(ctx context.Context, request *environmentpb.SetEnvironmentIdleSuspendRequest) (*emptypb.Empty, error) {
	access, err := s.manager(ctx)
	if err != nil {
		return nil, err
	}
	name, uid, err := s.mutationTarget(request.GetTarget())
	if err != nil {
		return nil, err
	}
	if err := access.SetIdleSuspendAfter(ctx, name, uid, request.GetIdleSuspendAfter()); err != nil {
		return nil, managementError(ctx, err)
	}
	return &emptypb.Empty{}, nil
}

func (s *Service) SetEnvironmentIdentity(ctx context.Context, request *environmentpb.SetEnvironmentIdentityRequest) (*emptypb.Empty, error) {
	access, err := s.manager(ctx)
	if err != nil {
		return nil, err
	}
	name, uid, err := s.mutationTarget(request.GetTarget())
	if err != nil {
		return nil, err
	}
	if s.identityChanges == nil {
		return nil, status.Error(codes.Unavailable, "Session identity admission is unavailable")
	}
	identity, err := access.ResolveIdentity(ctx, request.GetIdentityName())
	if err != nil {
		return nil, managementError(ctx, err)
	}
	if err := s.identityChanges.ChangeSandboxIdentity(ctx, uid, identity, func() error {
		return access.SetIdentity(ctx, name, uid, identity)
	}); err != nil {
		return nil, managementError(ctx, err)
	}
	return &emptypb.Empty{}, nil
}

func managementError(ctx context.Context, err error) error {
	var missing *management.SandboxNotFoundError
	var invalid *management.InvalidSandboxError
	var unknownPolicy *policy.UnknownPolicySetError
	var launcherPolicy *management.LauncherPolicySetError
	var conflict *management.SandboxConflictError
	var running *management.SandboxRunningError
	var identityRunning *management.IdentityChangeRunningError
	var unknown *management.SandboxModeUnknownError
	var teardown *management.SandboxTearingDownError
	var sessions interface{ ConflictingSessionIDs() []string }
	switch {
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(err).Err()
	case errors.As(err, &missing), apierrors.IsNotFound(err):
		return status.Error(codes.NotFound, err.Error())
	case errors.As(err, &unknownPolicy):
		return managementRefusal(codes.InvalidArgument, "UNKNOWN_POLICY_SET", err.Error())
	case errors.As(err, &launcherPolicy):
		return managementRefusal(codes.InvalidArgument, "LAUNCHER_POLICY_SET", err.Error())
	case errors.As(err, &invalid):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.As(err, &sessions), apierrors.IsInvalid(err):
		return status.Error(codes.FailedPrecondition, err.Error())
	case errors.As(err, &conflict), apierrors.IsAlreadyExists(err):
		return status.Error(codes.AlreadyExists, err.Error())
	case errors.As(err, &running), errors.As(err, &identityRunning), errors.As(err, &unknown), errors.As(err, &teardown), apierrors.IsConflict(err):
		return status.Error(codes.FailedPrecondition, err.Error())
	default:
		slog.ErrorContext(ctx, "environment management failed", "error", err)
		return status.Error(codes.Unavailable, "environment management is unavailable")
	}
}

func managementRefusal(code codes.Code, reason, message string) error {
	result, err := status.New(code, message).WithDetails(&errdetails.ErrorInfo{Reason: reason, Domain: "agentplane.environments"})
	if err != nil {
		return status.Error(codes.Internal, "could not encode Environment refusal")
	}
	return result.Err()
}
