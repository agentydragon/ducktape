package testing

import (
	"encoding/json"
	"os"
	"testing"

	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/google/uuid"
)

// Golden holds the retired Python service's accepted and rejected webhook vectors.
// internal/inbox/golden.json was written by generate_golden.py at commit 180ae8a62d.
type Golden struct {
	Deliveries         []GoldenDelivery `json:"deliveries"`
	RejectedDeliveries []struct {
		Name       string    `json:"name"`
		Event      string    `json:"event"`
		DeliveryID uuid.UUID `json:"delivery_id"`
		Raw        string    `json:"raw"`
		Reason     string    `json:"reason"`
	} `json:"rejected_deliveries"`
}

type GoldenDelivery struct {
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

func LoadGolden(t *testing.T, rlocation string) Golden {
	t.Helper()
	path, err := runfiles.Rlocation(rlocation)
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var vectors Golden
	if err := json.Unmarshal(data, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}
