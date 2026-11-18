package runtimeproxy

import (
	"context"
	"testing"

	environmentruntimepb "github.com/agentydragon/ducktape/agentplane/env/runtime/control/environmentruntimepb"
	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	sessionpb "github.com/agentydragon/ducktape/agentplane/proto/session"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

type diagnosticRuntime struct {
	observedRuntime
	read func() (*sessionpb.EnvironmentRuntimeStatus, error)
}

func (r *diagnosticRuntime) GetStatus(ctx context.Context, _ *sessionpb.GetStatusRequest) (*sessionpb.EnvironmentRuntimeStatus, error) {
	if err := r.validateFence(ctx); err != nil {
		return nil, err
	}
	md, _ := metadata.FromIncomingContext(ctx)
	if len(md.Get("authorization")) != 0 || len(md.Get("x-agentplane-sandbox")) != 0 {
		return nil, status.Error(codes.PermissionDenied, "caller metadata leaked")
	}
	r.calls.Add(1)
	return r.read()
}

func TestRuntimeDiagnosticsUsesFencedLiveConnection(t *testing.T) {
	f := newSandboxFixture(t)
	proxy := f.newServer(t)
	destination, err := proxy.runtimeDestination(t.Context(), f.sandbox)
	if err != nil {
		t.Fatal(err)
	}
	credentials, err := f.runtimeTLS.ServerCredentials(peertls.RuntimeProxyURI)
	if err != nil {
		t.Fatal(err)
	}
	snapshot := &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "diagnostic-boot", Processes: []*sessionpb.HarnessProcessStatus{{ProcessId: "process-1", AssignedSessionIds: []string{"session-a"}}}}
	expected := proto.Clone(snapshot).(*sessionpb.EnvironmentRuntimeStatus)
	legacyRow := protowire.AppendString(protowire.AppendTag(nil, 1, protowire.BytesType), "legacy-session")
	legacyRow = protowire.AppendVarint(protowire.AppendTag(legacyRow, 7, protowire.VarintType), 1)
	// An old Runtime still returns field 2 after Environments contracts its descriptor.
	snapshot.ProtoReflect().SetUnknown(protowire.AppendBytes(protowire.AppendTag(nil, 2, protowire.BytesType), legacyRow))
	runtime := &diagnosticRuntime{observedRuntime: observedRuntime{sandboxUID: string(f.sandbox.UID), incarnationUID: string(f.pod.UID), bootID: "diagnostic-boot"}, read: func() (*sessionpb.EnvironmentRuntimeStatus, error) { return snapshot, nil }}
	server := grpc.NewServer(grpc.Creds(credentials))
	environmentruntimepb.RegisterEnvironmentRuntimeServer(server, runtime)
	destination.target = grpcserver.Serve(t, server)
	cancel, done, _ := runObserver(t, proxy, destination)
	connection := awaitConnection(t, proxy, string(f.sandbox.UID), runtime.bootID, done)
	// Neither a previous client context nor untrusted proxy headers supply the Runtime fence.
	ctx := metadata.NewOutgoingContext(t.Context(), metadata.Pairs("authorization", "Bearer foreign", "x-agentplane-sandbox", "other"))
	got, err := proxy.RuntimeStatus(ctx, destination.identity, destination.podUID)
	if err != nil || !proto.Equal(got, expected) {
		t.Fatalf("live diagnostics = %v, %v", got, err)
	}
	for _, test := range []struct {
		target      *identitypb.EnvironmentTarget
		incarnation string
		code        codes.Code
	}{
		{nil, destination.podUID, codes.InvalidArgument},
		{&identitypb.EnvironmentTarget{Namespace: f.namespace, Name: f.sandbox.Name, Uid: "old-sandbox"}, destination.podUID, codes.NotFound},
		{destination.identity, "old-incarnation", codes.Aborted},
	} {
		if _, err := proxy.RuntimeStatus(ctx, test.target, test.incarnation); status.Code(err) != test.code {
			t.Fatalf("stale target/incarnation = %v", err)
		}
	}
	if runtime.calls.Load() != 1 {
		t.Fatal("stale identity reached Runtime")
	}
	runtime.read = func() (*sessionpb.EnvironmentRuntimeStatus, error) {
		return &sessionpb.EnvironmentRuntimeStatus{EnvironmentRuntimeBootId: "new-boot"}, nil
	}
	if _, err := proxy.RuntimeStatus(ctx, destination.identity, destination.podUID); status.Code(err) != codes.Unavailable {
		t.Fatalf("boot mismatch = %v", err)
	}
	runtime.read = func() (*sessionpb.EnvironmentRuntimeStatus, error) {
		proxy.connections.remove(connection, status.Error(codes.Unavailable, "watch lost"))
		return snapshot, nil
	}
	if _, err := proxy.RuntimeStatus(ctx, destination.identity, destination.podUID); status.Code(err) != codes.Unavailable {
		t.Fatalf("watch lost during read = %v", err)
	}
	cancel()
	waitObserver(t, done)
	if _, err := proxy.RuntimeStatus(ctx, destination.identity, destination.podUID); status.Code(err) != codes.Unavailable {
		t.Fatalf("offline Runtime = %v", err)
	}
}
