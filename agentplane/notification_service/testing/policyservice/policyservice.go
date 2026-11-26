// Package policyservice serves the Identities of a test API server as the Policy Controller's PolicyService, so a
// Notifications process under test reads Identities the way it does in production.
package policyservice

import (
	"context"
	"maps"
	"slices"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	peertlstesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	apierrors "k8s.io/apimachinery/pkg/api/errors"
	"k8s.io/apimachinery/pkg/runtime"
	"k8s.io/apimachinery/pkg/types"
	"k8s.io/client-go/rest"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// pollInterval is how often WatchIdentities re-reads the Identities; tests wait on the resulting changes.
const pollInterval = 100 * time.Millisecond

// Serve starts PolicyService over mutual TLS for Notifications, backed by the Identities of namespace on config's API
// server, and returns the `policy_service` peer that reaches it.
func Serve(t *testing.T, config *rest.Config, namespace string) identitycache.Peer {
	t.Helper()
	scheme := runtime.NewScheme()
	if err := policyapi.AddToScheme(scheme); err != nil {
		t.Fatal(err)
	}
	objects, err := client.New(config, client.Options{Scheme: scheme})
	if err != nil {
		t.Fatal(err)
	}
	ca := peertlstesting.NewCA(t)
	serverTLS := ca.Issue(t, peertls.PolicyControllerURI)
	clientTLS := ca.Issue(t, peertls.NotificationsURI)
	credentials, err := serverTLS.ServerCredentials(peertls.NotificationsURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials))
	policypb.RegisterPolicyServiceServer(server, &service{objects: objects, namespace: namespace})
	return identitycache.Peer{
		Target: grpcserver.Serve(t, server),
		TLS: peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.PolicyControllerURI,
			CertFile: clientTLS.CertFile, KeyFile: clientTLS.KeyFile, ClientURI: peertls.NotificationsURI,
		},
	}
}

type service struct {
	policypb.UnimplementedPolicyServiceServer
	objects   client.Client
	namespace string
}

func message(identity *policyapi.Identity) *policypb.Identity {
	out := &policypb.Identity{Identity: &policypb.PolicyResourceIdentity{
		Namespace: identity.Namespace, Name: identity.Name, Uid: string(identity.UID),
	}}
	if identity.DeletionTimestamp != nil {
		out.DeletionTimestamp = timestamppb.New(identity.DeletionTimestamp.Time)
	}
	return out
}

func (s *service) GetIdentity(ctx context.Context, request *policypb.GetIdentityRequest) (*policypb.GetIdentityResponse, error) {
	identity := &policyapi.Identity{}
	if err := s.objects.Get(ctx, types.NamespacedName{Namespace: s.namespace, Name: request.GetName()}, identity); err != nil {
		if apierrors.IsNotFound(err) {
			return nil, status.Error(codes.NotFound, "Identity not found")
		}
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	return &policypb.GetIdentityResponse{Identity: message(identity)}, nil
}

func (s *service) read(ctx context.Context) (map[string]*policypb.Identity, error) {
	var list policyapi.IdentityList
	if err := s.objects.List(ctx, &list, client.InNamespace(s.namespace)); err != nil {
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	out := make(map[string]*policypb.Identity, len(list.Items))
	for i := range list.Items {
		out[list.Items[i].Name] = message(&list.Items[i])
	}
	return out, nil
}

func (s *service) WatchIdentities(_ *policypb.WatchIdentitiesRequest, stream grpc.ServerStreamingServer[policypb.WatchIdentitiesResponse]) error {
	ctx := stream.Context()
	current, err := s.read(ctx)
	if err != nil {
		return err
	}
	snapshot := make([]*policypb.Identity, 0, len(current))
	for _, name := range slices.Sorted(maps.Keys(current)) {
		snapshot = append(snapshot, current[name])
	}
	for _, frame := range []*policypb.WatchIdentitiesResponse{
		{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: &policypb.IdentityChunk{Identities: snapshot}}},
		{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}},
	} {
		if err := stream.Send(frame); err != nil {
			return err
		}
	}
	ticker := time.NewTicker(pollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return status.FromContextError(ctx.Err()).Err()
		case <-ticker.C:
		}
		next, err := s.read(ctx)
		if err != nil {
			return err
		}
		for name, old := range current {
			if item, ok := next[name]; !ok || item.GetIdentity().GetUid() != old.GetIdentity().GetUid() {
				if err := stream.Send(&policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{Removed: old.GetIdentity()}}); err != nil {
					return err
				}
			}
		}
		for name, item := range next {
			if old, ok := current[name]; !ok || !proto.Equal(old, item) {
				if err := stream.Send(&policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: item}}); err != nil {
					return err
				}
			}
		}
		current = next
	}
}
