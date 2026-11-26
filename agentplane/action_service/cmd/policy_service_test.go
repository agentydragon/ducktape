package main

import (
	"context"
	"testing"
	"time"

	"github.com/agentydragon/ducktape/agentplane/net/peertls"
	transporttesting "github.com/agentydragon/ducktape/agentplane/net/peertls/testing"
	policyapi "github.com/agentydragon/ducktape/agentplane/policy/api/v1alpha1"
	"github.com/agentydragon/ducktape/agentplane/policy/identitycache"
	policypb "github.com/agentydragon/ducktape/agentplane/policy/v1"
	"github.com/agentydragon/ducktape/agentplane/testing/grpcserver"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/emptypb"
	"google.golang.org/protobuf/types/known/timestamppb"
	"sigs.k8s.io/controller-runtime/pkg/client"
)

// identityPollInterval is how often fakePolicyService looks for Identity changes on the API server.
const identityPollInterval = 50 * time.Millisecond

// fakePolicyService serves PolicyService's Identity reads from the Identities on an API server.
type fakePolicyService struct {
	policypb.UnimplementedPolicyServiceServer
	identities client.Client
}

func toProto(identity *policyapi.Identity) *policypb.Identity {
	converted := &policypb.Identity{Identity: &policypb.PolicyResourceIdentity{
		Namespace: identity.Namespace, Name: identity.Name, Uid: string(identity.UID),
	}}
	if identity.DeletionTimestamp != nil {
		converted.DeletionTimestamp = timestamppb.New(identity.DeletionTimestamp.Time)
	}
	return converted
}

func (f *fakePolicyService) GetIdentity(ctx context.Context, request *policypb.GetIdentityRequest) (*policypb.GetIdentityResponse, error) {
	var identity policyapi.Identity
	if err := f.identities.Get(ctx, client.ObjectKey{Namespace: policyNamespace, Name: request.GetName()}, &identity); err != nil {
		return nil, status.Error(codes.NotFound, err.Error())
	}
	return &policypb.GetIdentityResponse{Identity: toProto(&identity)}, nil
}

func (f *fakePolicyService) list(ctx context.Context) (map[string]*policypb.Identity, error) {
	var listed policyapi.IdentityList
	if err := f.identities.List(ctx, &listed, client.InNamespace(policyNamespace)); err != nil {
		return nil, err
	}
	current := map[string]*policypb.Identity{}
	for i := range listed.Items {
		current[listed.Items[i].Name] = toProto(&listed.Items[i])
	}
	return current, nil
}

// WatchIdentities sends a snapshot, then every Identity incarnation that appeared or disappeared since.
func (f *fakePolicyService) WatchIdentities(_ *policypb.WatchIdentitiesRequest, stream grpc.ServerStreamingServer[policypb.WatchIdentitiesResponse]) error {
	known, err := f.list(stream.Context())
	if err != nil {
		return status.Error(codes.Unavailable, err.Error())
	}
	snapshot := &policypb.IdentityChunk{}
	for _, identity := range known {
		snapshot.Identities = append(snapshot.Identities, identity)
	}
	for _, frame := range []*policypb.WatchIdentitiesResponse{
		{Frame: &policypb.WatchIdentitiesResponse_Snapshot{Snapshot: snapshot}},
		{Frame: &policypb.WatchIdentitiesResponse_SnapshotComplete{SnapshotComplete: &emptypb.Empty{}}},
	} {
		if err := stream.Send(frame); err != nil {
			return err
		}
	}
	ticker := time.NewTicker(identityPollInterval)
	defer ticker.Stop()
	for {
		select {
		case <-stream.Context().Done():
			return stream.Context().Err()
		case <-ticker.C:
		}
		current, err := f.list(stream.Context())
		if err != nil {
			return status.Error(codes.Unavailable, err.Error())
		}
		for name, identity := range current {
			if previous, ok := known[name]; !ok || previous.GetIdentity().GetUid() != identity.GetIdentity().GetUid() ||
				previous.GetDeletionTimestamp() == nil != (identity.GetDeletionTimestamp() == nil) {
				if err := stream.Send(&policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Upsert{Upsert: identity}}); err != nil {
					return err
				}
			}
		}
		for name, previous := range known {
			if _, ok := current[name]; !ok {
				if err := stream.Send(&policypb.WatchIdentitiesResponse{Frame: &policypb.WatchIdentitiesResponse_Removed{Removed: previous.GetIdentity()}}); err != nil {
					return err
				}
			}
		}
		known = current
	}
}

// servePolicyService serves a fakePolicyService over the API server's Identities to the Action Service's SVID alone,
// and returns the policy_service setting that reaches it.
func servePolicyService(t *testing.T, ca *transporttesting.CA, actions peertls.Config) identitycache.Peer {
	t.Helper()
	credentials, err := ca.Issue(t, peertls.PolicyControllerURI).ServerCredentials(peertls.ActionsURI)
	if err != nil {
		t.Fatal(err)
	}
	server := grpc.NewServer(grpc.Creds(credentials))
	policypb.RegisterPolicyServiceServer(server, &fakePolicyService{identities: workloadCredentials(t)})
	return identitycache.Peer{
		Target: grpcserver.Serve(t, server),
		TLS: peertls.ClientTLS{
			CAFile: ca.RootFile, ServerURI: peertls.PolicyControllerURI,
			CertFile: actions.CertFile, KeyFile: actions.KeyFile, ClientURI: peertls.ActionsURI,
		},
	}
}
