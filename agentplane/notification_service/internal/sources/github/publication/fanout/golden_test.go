package fanout

import (
	"encoding/hex"
	"encoding/json"
	"os"
	"testing"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.
	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	"github.com/agentydragon/ducktape/agentplane/testing/jsonassert"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/google/uuid"
)

var goldenRlocation string

type golden struct {
	Deliveries []goldenDelivery `json:"deliveries"`
}

type goldenDelivery struct {
	Name       string    `json:"name"`
	Event      string    `json:"event"`
	DeliveryID uuid.UUID `json:"delivery_id"`
	Raw        string    `json:"raw"`
	Ingest     struct {
		AppID          int64           `json:"app_id"`
		DeliveryID     uuid.UUID       `json:"delivery_id"`
		InstallationID int64           `json:"installation_id"`
		RepositoryID   *int64          `json:"repository_id"`
		Event          string          `json:"event"`
		Digest         string          `json:"digest"`
		Payload        json.RawMessage `json:"payload"`
		Action         *string         `json:"action"`
		HeadSHA        *string         `json:"head_sha"`
		Subjects       []struct {
			RepositoryID int64  `json:"repository_id"`
			Kind         string `json:"kind"`
			SubjectKey   string `json:"subject_key"`
		} `json:"subjects"`
		RepositoryName *string `json:"repository_name"`
		Revisions      []struct {
			RepositoryID   int64  `json:"repository_id"`
			RepositoryName string `json:"repository_name"`
			SHA            string `json:"sha"`
		} `json:"revisions"`
	} `json:"ingest"`
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	path, err := runfiles.Rlocation(goldenRlocation)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var vectors golden
	if err := json.Unmarshal(data, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

func (d goldenDelivery) ingest(t *testing.T) githubstate.Ingest {
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
