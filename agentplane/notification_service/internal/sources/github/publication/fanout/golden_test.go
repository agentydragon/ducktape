package fanout

import (
	"encoding/hex"
	"testing"

	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/testing" // Golden vector schema shared with webhook tests.

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/testing/jsonassert"
)

var goldenRlocation string

func goldenIngest(t *testing.T, d fixture.GoldenDelivery) githubstate.Ingest {
	t.Helper()
	digest, err := hex.DecodeString(d.Ingest.Digest)
	if err != nil {
		t.Fatal(err)
	}
	ingest := githubstate.Ingest{
		AppID: d.Ingest.AppID, DeliveryID: d.Ingest.DeliveryID, InstallationID: d.Ingest.InstallationID,
		Event: d.Ingest.Event, Digest: digest, Payload: d.Ingest.Payload, Action: d.Ingest.Action, HeadSHA: d.Ingest.HeadSHA,
	}
	if d.Ingest.RepositoryID != nil {
		ingest.Repository = &githubmodel.Repository{ID: *d.Ingest.RepositoryID, Name: *d.Ingest.RepositoryName}
	}
	for _, subject := range d.Ingest.Subjects {
		ingest.Subjects = append(ingest.Subjects, githubstate.SubjectKey{RepositoryID: subject.RepositoryID, Kind: subject.Kind, Key: subject.SubjectKey})
	}
	for _, revision := range d.Ingest.Revisions {
		ingest.Revisions = append(ingest.Revisions, githubmodel.HeadRevision{
			Repository: githubmodel.Repository{ID: revision.RepositoryID, Name: revision.RepositoryName}, SHA: revision.SHA,
		})
	}
	return ingest
}

// sameJSON compares JSONB values without considering object key order or whitespace.
func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	if !jsonassert.Equal(t, got, want) {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}
