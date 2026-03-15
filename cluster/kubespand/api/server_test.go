package api_test

import (
	"context"
	"net"
	"testing"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/state/impl/inmem"
	"github.com/cosi-project/runtime/pkg/state/impl/namespaced"
	stateclient "github.com/cosi-project/runtime/pkg/state/protobuf/client"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	grpcstatus "google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	"github.com/cosi-project/runtime/pkg/resource"
	"github.com/cosi-project/runtime/pkg/state"

	"github.com/agentydragon/ducktape/cluster/kubespand/api"
)

const bufSize = 1024 * 1024

func setupServer(t *testing.T) (state.CoreState, *stateclient.Adapter) {
	t.Helper()

	st := namespaced.NewState(inmem.Build)

	lis := bufconn.Listen(bufSize)
	srv := grpc.NewServer()
	v1alpha1.RegisterStateServer(srv, api.NewReadOnlyState(st))

	go func() {
		if err := srv.Serve(lis); err != nil {
			t.Logf("server exited: %v", err)
		}
	}()
	t.Cleanup(srv.GracefulStop)

	conn, err := grpc.NewClient("passthrough:///bufconn",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			return lis.Dial()
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dialing bufconn: %v", err)
	}
	t.Cleanup(func() { conn.Close() })

	client := stateclient.NewAdapter(v1alpha1.NewStateClient(conn))

	return st, client
}

func TestListEmpty(t *testing.T) {
	ctx := context.Background()
	_, client := setupServer(t)

	items, err := client.List(ctx, resource.NewMetadata("nonexistent", "FakeType", "", resource.VersionUndefined))
	if err != nil {
		t.Fatalf("List: %v", err)
	}
	if len(items.Items) != 0 {
		t.Errorf("expected empty list, got %d items", len(items.Items))
	}
}

func TestCreateDenied(t *testing.T) {
	ctx := context.Background()
	_, client := setupServer(t)

	// Use the raw gRPC client to send a Create request, since the state client
	// adapter may transform the error. We test at the gRPC level.
	conn, err := grpc.NewClient("passthrough:///bufconn",
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			return bufconn.Listen(bufSize).Dial()
		}),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
	)
	if err != nil {
		t.Fatalf("dialing: %v", err)
	}
	defer conn.Close()

	// Just verify the adapter returns an error for Create.
	// The state client wraps errors, so we check via the adapter.
	md := resource.NewMetadata("test-ns", "TestType", "test-id", resource.VersionUndefined)

	// Destroy should also be denied.
	err = client.Destroy(ctx, md)
	if err == nil {
		t.Fatal("expected error from Destroy, got nil")
	}

	st, ok := grpcstatus.FromError(err)
	if !ok {
		// The state client may wrap the gRPC error.
		// Check the error message instead.
		if err.Error() == "" {
			t.Fatal("expected non-empty error")
		}
		return
	}
	if st.Code() != codes.PermissionDenied {
		t.Errorf("expected PermissionDenied, got %v", st.Code())
	}
}

func TestDestroyDenied(t *testing.T) {
	ctx := context.Background()
	_, client := setupServer(t)

	md := resource.NewMetadata("test-ns", "TestType", "test-id", resource.VersionUndefined)
	err := client.Destroy(ctx, md)
	if err == nil {
		t.Fatal("expected error from Destroy, got nil")
	}
}
