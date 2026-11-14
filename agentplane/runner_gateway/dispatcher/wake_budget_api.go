package dispatcher

import (
	"context"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	wakebudgetpb "github.com/agentydragon/ducktape/agentplane/runner_gateway/dispatcher/api/wakebudget/wakebudgetpb"
	"github.com/agentydragon/ducktape/agentplane/runner_gateway/environments/lifecycle"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/durationpb"
)

// WakeBudgetService exposes only the existing durable reservation to a configured controller Pod.
// The controller checks wake policy; this endpoint never grants wake or patches a Sandbox.
type WakeBudgetService struct {
	wakebudgetpb.UnimplementedWakeBudgetServer
	Store             *Store
	ControllerAccount *auth.Account
}

func (s *WakeBudgetService) ReserveWakeAttempt(ctx context.Context, request *wakebudgetpb.ReserveWakeAttemptRequest) (*wakebudgetpb.ReserveWakeAttemptResponse, error) {
	caller, err := auth.AdmittedReviewedCaller(ctx)
	if err != nil {
		return nil, err
	}
	if s.ControllerAccount == nil || caller.Pod == nil || caller.Pod.Account != *s.ControllerAccount {
		return nil, status.Error(codes.PermissionDenied, "wake budget caller is not the configured controller workload")
	}
	if request.GetCallerNamespace() == "" || request.GetCallerName() == "" || request.GetCallerUid() == "" ||
		request.GetSandboxUid() == "" || request.GetWakeGeneration() < 1 {
		return nil, status.Error(codes.InvalidArgument, "wake budget reservation requires a caller incarnation, Sandbox UID and positive generation")
	}
	if s.Store == nil {
		return nil, status.Error(codes.Unavailable, "wake budget storage unavailable")
	}
	wait, err := s.Store.ReserveWakeAttempt(ctx, lifecycle.WakeBudgetKey{
		Namespace: request.GetCallerNamespace(), Name: request.GetCallerName(), UID: request.GetCallerUid(),
	}, request.GetSandboxUid(), request.GetWakeGeneration())
	if err != nil {
		if ctx.Err() != nil {
			return nil, status.FromContextError(ctx.Err()).Err()
		}
		return nil, status.Error(codes.Unavailable, "wake budget storage unavailable")
	}
	return &wakebudgetpb.ReserveWakeAttemptResponse{RetryAfter: durationpb.New(wait)}, nil
}

// WakeBudgetMethods admits direct reviewed ServiceAccount credentials only. The handler additionally
// requires the configured account and Pod binding; delegation and SVIDs have no route to this method.
func WakeBudgetMethods(resolver *auth.Resolver) auth.Methods {
	return auth.Methods{wakebudgetpb.WakeBudget_ReserveWakeAttempt_FullMethodName: auth.ReviewedServiceAccount(resolver)}
}
