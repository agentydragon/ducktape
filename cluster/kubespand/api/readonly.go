// Package api exposes kubespand's COSI state on a Unix socket.
//
// Mirrors Talos machined's state socket at /system/run/machined/machine.sock.
// The state is read-only — write operations (Create, Update, Destroy) return
// PermissionDenied. A future apid integration will add mTLS on port 50000.
package api

import (
	"context"

	v1alpha1 "github.com/cosi-project/runtime/api/v1alpha1"
	"github.com/cosi-project/runtime/pkg/state"
	stateserver "github.com/cosi-project/runtime/pkg/state/protobuf/server"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

// ReadOnlyState wraps the COSI gRPC state server to reject write operations.
// Read operations (Get, List, Watch) delegate to the underlying state server.
// Write operations (Create, Update, Destroy) return PermissionDenied.
type ReadOnlyState struct {
	v1alpha1.UnimplementedStateServer
	inner *stateserver.State
}

// NewReadOnlyState creates a read-only wrapper around a COSI state.
func NewReadOnlyState(st state.CoreState) *ReadOnlyState {
	return &ReadOnlyState{inner: stateserver.NewState(st)}
}

func (s *ReadOnlyState) Get(ctx context.Context, req *v1alpha1.GetRequest) (*v1alpha1.GetResponse, error) {
	return s.inner.Get(ctx, req)
}

func (s *ReadOnlyState) List(req *v1alpha1.ListRequest, srv grpc.ServerStreamingServer[v1alpha1.ListResponse]) error {
	return s.inner.List(req, srv)
}

func (s *ReadOnlyState) Watch(req *v1alpha1.WatchRequest, srv grpc.ServerStreamingServer[v1alpha1.WatchResponse]) error {
	return s.inner.Watch(req, srv)
}

func (s *ReadOnlyState) Create(context.Context, *v1alpha1.CreateRequest) (*v1alpha1.CreateResponse, error) {
	return nil, status.Errorf(codes.PermissionDenied, "kubespand API is read-only")
}

func (s *ReadOnlyState) Update(context.Context, *v1alpha1.UpdateRequest) (*v1alpha1.UpdateResponse, error) {
	return nil, status.Errorf(codes.PermissionDenied, "kubespand API is read-only")
}

func (s *ReadOnlyState) Destroy(context.Context, *v1alpha1.DestroyRequest) (*v1alpha1.DestroyResponse, error) {
	return nil, status.Errorf(codes.PermissionDenied, "kubespand API is read-only")
}
