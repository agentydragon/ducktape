package admin

import (
	"errors"
	"fmt"
	"strings"
	"testing"

	egressadminpb "github.com/agentydragon/ducktape/agentplane/egress/egress_adminpb"
	"github.com/agentydragon/ducktape/agentplane/egress/policy"
	"github.com/agentydragon/ducktape/agentplane/egress/resources"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protojson"
)

func TestWatchCredentialsRedactedUpdatesAndHealth(t *testing.T) {
	index := policy.NewIndex()
	index.Health.MarkSynced()
	index.PutCredential(resources.Credential{
		UID: "uid-1", Name: "github", Description: "GitHub API access", Source: resources.SecretKey{Name: "private-secret", Key: "private-key"},
		Targets: []resources.Target{resources.SchemeToken{Header: "Authorization", Scheme: "Bearer"}},
	})
	client, _ := serveGRPC(t, ServiceOptions{Credentials: index, Index: index.Health})
	stream, err := client.WatchCredentials(bearing(readerToken), &egressadminpb.WatchCredentialsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	receive := func() *egressadminpb.WatchCredentialsResponse {
		t.Helper()
		response, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		return response
	}
	snapshot := receive()
	if len(snapshot.GetSnapshot().GetCredentials()) != 1 || snapshot.GetSnapshot().GetCredentials()[0].GetDescription() != "GitHub API access" || snapshot.GetSnapshot().GetCredentials()[0].GetTargets()[0].GetScheme() != "Bearer" {
		t.Fatalf("snapshot = %v", snapshot)
	}
	if done := receive().GetSnapshotComplete(); done == nil {
		t.Fatal("snapshot did not complete")
	}
	encoded, err := protojson.Marshal(snapshot)
	if err != nil {
		t.Fatal(err)
	}
	if strings.Contains(string(encoded), "private-secret") || strings.Contains(string(encoded), "private-key") {
		t.Fatalf("source leaked: %s", encoded)
	}
	index.PutCredential(resources.Credential{UID: "uid-1", Name: "github", Description: "Updated", Source: resources.SecretKey{Name: "keys", Key: "token"}, Targets: []resources.Target{resources.WholeValue{Header: "x-api-key"}}})
	if got := receive().GetUpsert().GetDescription(); got != "Updated" {
		t.Fatalf("updated description = %q", got)
	}
	index.DeleteCredential("github")
	if got := receive().GetRemoved(); got.GetName() != "github" || got.GetUid() != "uid-1" {
		t.Fatalf("removal = %v", got)
	}
	index.Health.RecordFailure(policy.Credentials, errors.New("watch failed"))
	if _, err := stream.Recv(); status.Code(err) != codes.Unavailable {
		t.Fatalf("failed watch error = %v", err)
	}
}

func TestWatchCredentialsChunksInitialSnapshot(t *testing.T) {
	index := policy.NewIndex()
	index.Health.MarkSynced()
	for i := range 130 {
		index.PutCredential(resources.Credential{
			UID: fmt.Sprintf("uid-%03d", i), Name: fmt.Sprintf("name-%03d", i), Description: "access",
			Source: resources.SecretKey{Name: "keys", Key: "token"}, Targets: []resources.Target{resources.WholeValue{Header: "x-api-key"}},
		})
	}
	client, _ := serveGRPC(t, ServiceOptions{Credentials: index, Index: index.Health})
	stream, err := client.WatchCredentials(bearing(readerToken), &egressadminpb.WatchCredentialsRequest{})
	if err != nil {
		t.Fatal(err)
	}
	for _, want := range []int{128, 2} {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if got := len(frame.GetSnapshot().GetCredentials()); got != want {
			t.Fatalf("snapshot chunk size = %d, want %d", got, want)
		}
	}
	frame, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if frame.GetSnapshotComplete() == nil {
		t.Fatalf("missing snapshot completion: %v", frame)
	}
}
