package service

import (
	"context"
	"testing"
	"time"

	environmentpb "github.com/agentydragon/ducktape/agentplane/environments/v1"
	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	sandboxapi "github.com/agentydragon/ducktape/agentplane/sandboxes/api"
	"github.com/agentydragon/ducktape/agentplane/sandboxes/runtimeconfig"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcmethods"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

type diagnosticRuntime struct {
	t      *testing.T
	target *identitypb.EnvironmentTarget
	calls  int
	read   func() (*sessionpb.EnvironmentRuntimeStatus, error)
}

func (r *diagnosticRuntime) RuntimeStatus(_ context.Context, target *identitypb.EnvironmentTarget, incarnation string) (*sessionpb.EnvironmentRuntimeStatus, error) {
	r.t.Helper()
	r.calls++
	if !proto.Equal(target, r.target) || incarnation != "pod-uid" {
		r.t.Fatalf("diagnostic fence = %v, %q", target, incarnation)
	}
	return r.read()
}

func TestRuntimeDiagnosticsPeerAndIncarnation(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Status.Incarnation = &sandboxapi.Incarnation{Name: "pod", UID: "pod-uid"}
	objects := readerWithObjects(t, sandbox)
	target := &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}
	snapshot := &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "boot-id", Processes: []*sessionpb.HarnessProcessStatus{{ProcessId: "process-1", AssignedSessionIds: []string{"session-a"}}}}
	runtime := &diagnosticRuntime{t: t, target: target, read: func() (*sessionpb.EnvironmentRuntimeStatus, error) { return snapshot, nil }}
	rpc, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), Options{RuntimeDiagnostics: runtime})
	if err != nil {
		t.Fatal(err)
	}
	// Even granting every other role to Notifications cannot grant diagnostics.
	methods, err := WithServiceCallers(Methods(nil), []string{peertls.NotificationsURI}, []string{peertls.NotificationsURI}, nil, nil)
	if err != nil {
		t.Fatal(err)
	}
	connection := serveFixture(t, methods, func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, rpc) })
	rpcClient := environmentpb.NewEnvironmentServiceClient(connection)
	request := &environmentpb.GetRuntimeDiagnosticsRequest{Target: target}
	start := time.Now()
	got, err := rpcClient.GetRuntimeDiagnostics(serviceContext(t, peertls.AppURI), request)
	if err != nil || !proto.Equal(got.GetTarget(), target) || got.GetIncarnationUid() != "pod-uid" || !proto.Equal(got.GetStatus(), snapshot) || got.GetObservedAt().AsTime().Before(start) {
		t.Fatalf("diagnostics = %v, %v", got, err)
	}
	for _, peer := range []string{peertls.NotificationsURI, peertls.ActionsURI, peertls.RuntimeProxyURI, ""} {
		ctx := grpcmethods.Caller(t.Context(), peer)
		if _, err := rpcClient.GetRuntimeDiagnostics(ctx, request); status.Code(err) != codes.PermissionDenied && status.Code(err) != codes.Unauthenticated {
			t.Fatalf("peer %q diagnostics = %v", peer, err)
		}
	}
	bearer := metadata.AppendToOutgoingContext(serviceContext(t, peertls.AppURI), "authorization", "Bearer retired")
	if _, err := rpcClient.GetRuntimeDiagnostics(bearer, request); status.Code(err) != codes.Unauthenticated {
		t.Fatalf("App plus bearer = %v", err)
	}
	delegated := delegatedServiceContext(t, peertls.AppURI, auth.Identity{Name: "owner", UID: "owner-uid"})
	if _, err := rpcClient.GetRuntimeDiagnostics(delegated, request); status.Code(err) != codes.PermissionDenied {
		t.Fatalf("delegated App diagnostics = %v", err)
	}
	if runtime.calls != 1 {
		t.Fatalf("unauthorized calls reached Runtime: %d", runtime.calls)
	}
	for _, test := range []struct {
		target *identitypb.EnvironmentTarget
		code   codes.Code
	}{
		{nil, codes.InvalidArgument},
		{&identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName}, codes.InvalidArgument},
		{&identitypb.EnvironmentTarget{Namespace: foreignNamespace, Name: sandboxName, Uid: sandboxUID}, codes.NotFound},
		{&identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: "old-uid"}, codes.NotFound},
	} {
		if _, err := rpcClient.GetRuntimeDiagnostics(serviceContext(t, peertls.AppURI), &environmentpb.GetRuntimeDiagnosticsRequest{Target: test.target}); status.Code(err) != test.code {
			t.Fatalf("target %v = %v", test.target, err)
		}
	}
	runtime.read = func() (*sessionpb.EnvironmentRuntimeStatus, error) {
		return nil, status.Error(codes.Unavailable, "offline")
	}
	if _, err := rpcClient.GetRuntimeDiagnostics(serviceContext(t, peertls.AppURI), request); status.Code(err) != codes.Unavailable {
		t.Fatalf("offline Runtime = %v", err)
	}
	runtime.read = func() (*sessionpb.EnvironmentRuntimeStatus, error) {
		current := &sandboxapi.Sandbox{}
		if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), current); err != nil {
			t.Fatal(err)
		}
		current.Status.Incarnation.UID = "replaced-pod"
		if err := objects.Update(t.Context(), current); err != nil {
			t.Fatal(err)
		}
		return snapshot, nil
	}
	if _, err := rpcClient.GetRuntimeDiagnostics(serviceContext(t, peertls.AppURI), request); status.Code(err) != codes.Aborted {
		t.Fatalf("replacement during read = %v", err)
	}
}

func TestRuntimeDiagnosticsUnavailableDoesNotResume(t *testing.T) {
	sandbox := sandboxObject(sandboxUID)
	sandbox.Spec.Running = false
	sandbox.Status.OperatingMode = sandboxapi.OperatingModeSuspended
	objects := readerWithObjects(t, sandbox)
	rpc, err := New(discoveryNamespace, objects, kubernetesIdentities(t, objects, discoveryNamespace), runtimeconfig.Catalog{}, discoveryLifecycle(t, objects, nil), Options{})
	if err != nil {
		t.Fatal(err)
	}
	rpcClient := environmentpb.NewEnvironmentServiceClient(serveFixture(t, Methods(nil), func(server *grpc.Server) { environmentpb.RegisterEnvironmentServiceServer(server, rpc) }))
	if _, err := rpcClient.GetRuntimeDiagnostics(serviceContext(t, peertls.AppURI), &environmentpb.GetRuntimeDiagnosticsRequest{Target: &identitypb.EnvironmentTarget{Namespace: discoveryNamespace, Name: sandboxName, Uid: sandboxUID}}); status.Code(err) != codes.Unavailable {
		t.Fatalf("no Runtime = %v", err)
	}
	current := &sandboxapi.Sandbox{}
	if err := objects.Get(t.Context(), client.ObjectKeyFromObject(sandbox), current); err != nil {
		t.Fatal(err)
	}
	if current.Spec.Running {
		t.Fatal("diagnostics resumed the environment")
	}
}
