package sandboxes

import (
	"context"
	"errors"
	"fmt"

	environmentpb "github.com/agentydragon/ducktape/agentplane/sandboxes/environments/environmentpb"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
)

const environmentPageSize = 100

// EnvironmentService is the read surface used by app callers. It deliberately exposes the
// generated service methods so reads preserve EnvironmentService authorization and UID fencing.
type EnvironmentService interface {
	GetEnvironment(context.Context, *environmentpb.GetEnvironmentRequest, ...grpc.CallOption) (*environmentpb.EnvironmentDescriptor, error)
	ListEnvironments(context.Context, *environmentpb.ListEnvironmentsRequest, ...grpc.CallOption) (*environmentpb.ListEnvironmentsResponse, error)
}

// EnvironmentReader resolves the app's name-only routes through the service's authorized
// inventory, then fetches the selected UID exactly. It never reads or reconstructs a Sandbox
// descriptor from Kubernetes for a public read.
type EnvironmentReader struct {
	namespace string
	service   EnvironmentService
}

// NewEnvironmentReader creates a reader scoped to the app's configured Sandbox namespace.
func NewEnvironmentReader(namespace string, service EnvironmentService) (*EnvironmentReader, error) {
	if namespace == "" || service == nil {
		return nil, errors.New("Sandbox namespace and Environment Service are required")
	}
	return &EnvironmentReader{namespace: namespace, service: service}, nil
}

// List returns the complete caller-visible inventory in the app's configured namespace.
func (r *EnvironmentReader) List(ctx context.Context) ([]*environmentpb.EnvironmentDescriptor, error) {
	var result []*environmentpb.EnvironmentDescriptor
	pageToken := ""
	seenTokens := map[string]struct{}{}
	for {
		response, err := r.service.ListEnvironments(ctx, &environmentpb.ListEnvironmentsRequest{
			Limit: environmentPageSize, PageToken: pageToken,
		})
		if err != nil {
			return nil, fmt.Errorf("list caller-visible Environments: %w", err)
		}
		if response == nil {
			return nil, status.Error(codes.Internal, "Environment Service returned an empty list response")
		}
		for _, descriptor := range response.GetItems() {
			if descriptor == nil || descriptor.GetTarget() == nil || descriptor.GetTarget().GetName() == "" || descriptor.GetTarget().GetUid() == "" {
				return nil, status.Error(codes.Internal, "Environment Service returned an incomplete target identity")
			}
			if descriptor.GetTarget().GetNamespace() == r.namespace {
				result = append(result, descriptor)
			}
		}
		next := response.GetNextPageToken()
		if next == "" {
			return result, nil
		}
		if _, exists := seenTokens[next]; exists || next == pageToken {
			return nil, status.Error(codes.Internal, "Environment Service returned a repeated page token")
		}
		seenTokens[next] = struct{}{}
		pageToken = next
	}
}

// Get resolves a name through the service inventory and then requests that exact target UID.
// If replacement occurs between the two RPCs, GetEnvironment's stale-target error is preserved.
func (r *EnvironmentReader) Get(ctx context.Context, name string) (*environmentpb.EnvironmentDescriptor, error) {
	if name == "" {
		return nil, status.Error(codes.InvalidArgument, "Environment name is required")
	}
	environments, err := r.List(ctx)
	if err != nil {
		return nil, err
	}
	var target *environmentpb.TargetHandle
	for _, descriptor := range environments {
		if descriptor.GetTarget().GetName() != name {
			continue
		}
		if target != nil {
			return nil, status.Errorf(codes.Internal, "Environment Service returned duplicate target name %q", name)
		}
		target = descriptor.GetTarget()
	}
	if target == nil {
		return nil, status.Errorf(codes.NotFound, "Environment %q was not found", name)
	}
	return r.GetExact(ctx, target)
}

// GetExactUID addresses the configured namespace by name and caller-known UID. It does not list
// or follow a same-name replacement.
func (r *EnvironmentReader) GetExactUID(ctx context.Context, name, uid string) (*environmentpb.EnvironmentDescriptor, error) {
	return r.GetExact(ctx, &environmentpb.TargetHandle{Namespace: r.namespace, Name: name, Uid: uid})
}

// GetExact fetches an already-identified target without resolving or replacing its UID.
func (r *EnvironmentReader) GetExact(ctx context.Context, target *environmentpb.TargetHandle) (*environmentpb.EnvironmentDescriptor, error) {
	if target == nil || target.GetNamespace() != r.namespace || target.GetName() == "" || target.GetUid() == "" {
		return nil, status.Error(codes.InvalidArgument, "complete Environment target identity is required")
	}
	descriptor, err := r.service.GetEnvironment(ctx, &environmentpb.GetEnvironmentRequest{Target: target})
	if err != nil {
		return nil, fmt.Errorf("get exact Environment %s/%s UID %q: %w", target.GetNamespace(), target.GetName(), target.GetUid(), err)
	}
	if descriptor == nil || descriptor.GetTarget() == nil ||
		descriptor.GetTarget().GetNamespace() != target.GetNamespace() ||
		descriptor.GetTarget().GetName() != target.GetName() ||
		descriptor.GetTarget().GetUid() != target.GetUid() {
		return nil, status.Error(codes.Internal, "Environment Service returned a different target identity")
	}
	return descriptor, nil
}

var _ EnvironmentService = environmentpb.EnvironmentServiceClient(nil)
