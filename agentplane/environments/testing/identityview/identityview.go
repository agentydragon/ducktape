// Package identityview serves the PolicyService Identity view EnvironmentService reads, backed by the Identity
// resources of a test API server, for tests that create Identities there.
package identityview

import (
	"context"
	"fmt"
	"net"

	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	metav1 "k8s.io/apimachinery/pkg/apis/meta/v1"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/watch"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// Start runs an Identity view of namespace's Identity resources until ctx ends and returns it.
func Start(ctx context.Context, config *rest.Config, namespace string) (*identitycache.Cache, error) {
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		return nil, err
	}
	objects, err := client.NewWithWatch(config, client.Options{Scheme: scheme})
	if err != nil {
		return nil, err
	}
	server := grpc.NewServer()
	policypb.RegisterPolicyServiceServer(server, &service{objects: objects, namespace: namespace})
	listener := bufconn.Listen(1 << 20)
	go func() { _ = server.Serve(listener) }()
	conn, err := grpc.NewClient("passthrough:///bufconn",
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }))
	if err != nil {
		server.Stop()
		return nil, err
	}
	cache, err := identitycache.New(policypb.NewPolicyServiceClient(conn), identitycache.StandardGrace)
	if err != nil {
		_ = conn.Close()
		server.Stop()
		return nil, err
	}
	go func() {
		_ = cache.Run(ctx)
		_ = conn.Close()
		server.Stop()
	}()
	return cache, nil
}

type service struct {
	policypb.UnimplementedPolicyServiceServer
	objects   client.WithWatch
	namespace string
}

func (s *service) GetIdentity(ctx context.Context, request *policypb.GetIdentityRequest) (*policypb.GetIdentityResponse, error) {
	var identity policyapi.Identity
	err := s.objects.Get(ctx, client.ObjectKey{Namespace: s.namespace, Name: request.GetName()}, &identity)
	if apierrors.IsNotFound(err) {
		return nil, status.Error(codes.NotFound, "Identity not found")
	}
	if err != nil {
		return nil, err
	}
	return &policypb.GetIdentityResponse{Identity: identityProto(&identity)}, nil
}

func (s *service) WatchIdentities(_ *policypb.WatchIdentitiesRequest, stream grpc.ServerStreamingServer[policypb.WatchIdentitiesResponse]) error {
	var list policyapi.IdentityList
	if err := s.objects.List(stream.Context(), &list, client.InNamespace(s.namespace)); err != nil {
		return err
	}
	snapshot := &policypb.IdentityChunk{}
	for i := range list.Items {
		snapshot.Identities = append(snapshot.Identities, identityProto(&list.Items[i]))
	}
	if err := stream.Send(&policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: snapshot}}); err != nil {
		return err
	}
	if err := stream.Send(&policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}}); err != nil {
		return err
	}
	events, err := s.objects.Watch(stream.Context(), &policyapi.IdentityList{}, &client.ListOptions{
		Namespace: s.namespace, Raw: &metav1.ListOptions{ResourceVersion: list.ResourceVersion},
	})
	if err != nil {
		return err
	}
	defer events.Stop()
	for event := range events.ResultChan() {
		identity, ok := event.Object.(*policyapi.Identity)
		if !ok {
			return fmt.Errorf("identityview: watch ended with %s event", event.Type)
		}
		frame := &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: identityProto(identity)}}
		if event.Type == watch.Deleted {
			frame = &policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{Removed: identityProto(identity).GetIdentity()}}
		}
		if err := stream.Send(frame); err != nil {
			return err
		}
	}
	return stream.Context().Err()
}

func identityProto(identity *policyapi.Identity) *policypb.Identity {
	out := &policypb.Identity{Identity: &policypb.PolicyResourceIdentity{
		Namespace: identity.Namespace, Name: identity.Name, Uid: string(identity.UID),
	}}
	if identity.DeletionTimestamp != nil {
		out.DeletionTimestamp = timestamppb.New(identity.DeletionTimestamp.Time)
	}
	return out
}
