package grpcmethods

import (
	"context"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/identity/auth"
	delegationpb "github.com/agentydragon/ducktape/agentplane/proto/delegation"
	identitypb "github.com/agentydragon/ducktape/agentplane/proto/identity"
	"google.golang.org/grpc/metadata"
)

// WithActionDelegation adds the delegation envelope of an Action execution acting as identity to ctx's outgoing
// metadata. Its request and execution IDs are fixed.
func WithActionDelegation(t *testing.T, ctx context.Context, identity auth.Identity) context.Context {
	t.Helper()
	encoded, err := auth.MarshalDelegation(&delegationpb.Delegation{
		EffectiveCaller: &delegationpb.Delegation_Identity{Identity: &identitypb.Identity{Name: identity.Name, Uid: identity.UID}},
		Provenance: &delegationpb.Delegation_Action{Action: &delegationpb.ActionProvenance{
			RequestId: "00000000-0000-4000-8000-000000000001", ExecutionId: "00000000-0000-4000-8000-000000000002",
		}},
	})
	if err != nil {
		t.Fatal(err)
	}
	return metadata.AppendToOutgoingContext(ctx, auth.DelegationMetadataKey, string(encoded))
}
