package service

import (
	"context"
	"log/slog"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/sandboxreader"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// RuntimeDiagnostics is the internal fenced read supplied by the replica-local Runtime transport.
type RuntimeDiagnostics interface {
	RuntimeStatus(context.Context, *identitypb.EnvironmentTarget, string) (*sessionpb.EnvironmentRuntimeStatus, error)
}

// GetRuntimeDiagnostics reads live process and lifecycle evidence without waking or
// mutating the environment. The App checks operator authority at its browser boundary.
func (s *Service) GetRuntimeDiagnostics(ctx context.Context, request *environmentpb.GetRuntimeDiagnosticsRequest) (*environmentpb.GetRuntimeDiagnosticsResponse, error) {
	admission, err := auth.Admitted(ctx)
	if err != nil {
		return nil, err
	}
	service, ok := admission.(auth.ServiceAdmission)
	if !ok || service.Caller.SPIFFEID != peertls.AppURI {
		return nil, status.Error(codes.PermissionDenied, "Runtime diagnostics require the App service peer")
	}
	target := request.GetTarget()
	if target.GetNamespace() == "" || target.GetName() == "" || target.GetUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "complete target handle is required")
	}
	if target.GetNamespace() != s.namespace {
		return nil, status.Error(codes.NotFound, "environment not found")
	}
	before, err := s.diagnosticEnvironment(ctx, target)
	if err != nil {
		return nil, err
	}
	if s.runtimeDiagnostics == nil || before.Resource.Status.Incarnation == nil {
		return nil, status.Error(codes.Unavailable, "Runtime diagnostics are unavailable")
	}
	incarnation := before.Resource.Status.Incarnation.UID
	snapshot, err := s.runtimeDiagnostics.RuntimeStatus(ctx, target, incarnation)
	if err != nil {
		return nil, err
	}
	after, err := s.diagnosticEnvironment(ctx, target)
	if err != nil {
		return nil, err
	}
	if after.Resource.Status.Incarnation == nil || after.Resource.Status.Incarnation.UID != incarnation {
		return nil, status.Error(codes.Aborted, "environment incarnation changed")
	}
	return &environmentpb.GetRuntimeDiagnosticsResponse{
		Target: proto.Clone(target).(*identitypb.EnvironmentTarget), IncarnationUid: incarnation,
		ObservedAt: timestamppb.New(time.Now().UTC()), Status: snapshot,
	}, nil
}

func (s *Service) diagnosticEnvironment(ctx context.Context, target *sandboxreader.Identity) (sandboxreader.View, error) {
	view, err := s.sandboxes.Get(ctx, target.GetName())
	if status.Code(err) == codes.NotFound || err == nil && view.Identity.Uid != target.GetUid() {
		return sandboxreader.View{}, status.Error(codes.NotFound, "environment not found")
	}
	if err != nil {
		slog.ErrorContext(ctx, "Runtime diagnostics Sandbox read failed", "error", err)
		return sandboxreader.View{}, status.Error(codes.Unavailable, "Sandbox inventory is unavailable")
	}
	return view, nil
}
