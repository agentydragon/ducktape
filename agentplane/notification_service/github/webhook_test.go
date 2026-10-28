package github

import (
	"bytes"
	"encoding/hex"
	"encoding/json"
	"errors"
	"os"
	"reflect"
	"testing"

	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/github/testing" // Shared fake API server; avoids colliding with Go testing.
	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	"github.com/agentydragon/ducktape/agentplane/notification_service/store"
	"github.com/bazelbuild/rules_go/go/runfiles"
	"github.com/google/uuid"
)

// The vectors are what the retired Python service stored for the webhook bodies it accepted, and why it refused the
// others (`store/golden.json`, written by `generate_golden.py` at commit 180ae8a62d).
type deliveryVectors struct {
	Deliveries []struct {
		Name       string    `json:"name"`
		Event      string    `json:"event"`
		DeliveryID uuid.UUID `json:"delivery_id"`
		Raw        string    `json:"raw"`
		Ingest     struct {
			AppID          int64           `json:"app_id"`
			InstallationID int64           `json:"installation_id"`
			RepositoryID   *int64          `json:"repository_id"`
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
	} `json:"deliveries"`
	RejectedDeliveries []struct {
		Name       string    `json:"name"`
		Event      string    `json:"event"`
		DeliveryID uuid.UUID `json:"delivery_id"`
		Raw        string    `json:"raw"`
		Reason     string    `json:"reason"`
	} `json:"rejected_deliveries"`
}

func loadVectors(t *testing.T) deliveryVectors {
	t.Helper()
	path, err := runfiles.Rlocation("_main/agentplane/notification_service/store/golden.json")
	if err != nil {
		t.Fatal(err)
	}
	data, err := os.ReadFile(path)
	if err != nil {
		t.Fatal(err)
	}
	var vectors deliveryVectors
	if err := json.Unmarshal(data, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

// sameJSON compares two documents as values, for what a JSONB column keeps: not key order, whitespace or
// duplicate keys.
func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	parse := func(data []byte) any {
		decoder := json.NewDecoder(bytes.NewReader(data))
		decoder.UseNumber()
		var value any
		if err := decoder.Decode(&value); err != nil {
			t.Fatalf("%s: %v", data, err)
		}
		return value
	}
	if !reflect.DeepEqual(parse(got), parse(want)) {
		t.Errorf("payload %s, want %s", got, want)
	}
}

func TestDeliveriesPythonAcceptsAreStoredAsPythonStoresThem(t *testing.T) {
	vectors := loadVectors(t)
	if len(vectors.Deliveries) == 0 {
		t.Fatal("no vectors")
	}
	for _, vector := range vectors.Deliveries {
		t.Run(vector.Name, func(t *testing.T) {
			got, err := ParseDelivery(vector.Ingest.AppID, vector.Event, vector.DeliveryID, []byte(vector.Raw))
			if err != nil {
				t.Fatal(err)
			}
			digest, err := hex.DecodeString(vector.Ingest.Digest)
			if err != nil {
				t.Fatal(err)
			}
			want := store.GitHubIngest{
				AppID: vector.Ingest.AppID, DeliveryID: vector.DeliveryID, InstallationID: vector.Ingest.InstallationID,
				Event: vector.Event, Digest: digest, Action: vector.Ingest.Action, HeadSHA: vector.Ingest.HeadSHA,
			}
			if vector.Ingest.RepositoryID != nil {
				want.Repository = &model.GitHubRepository{ID: *vector.Ingest.RepositoryID, Name: *vector.Ingest.RepositoryName}
			}
			for _, subject := range vector.Ingest.Subjects {
				want.Subjects = append(want.Subjects, store.GitHubSubjectKey{
					RepositoryID: subject.RepositoryID, Kind: subject.Kind, Key: subject.SubjectKey,
				})
			}
			for _, revision := range vector.Ingest.Revisions {
				want.Revisions = append(want.Revisions, model.GitHubHeadRevision{
					Repository: model.GitHubRepository{ID: revision.RepositoryID, Name: revision.RepositoryName}, SHA: revision.SHA,
				})
			}

			sameJSON(t, got.Payload, vector.Ingest.Payload)
			got.Payload = nil
			// These vectors pin raw receipt correlation. Complete current-state facts have separate tests.
			got.CheckRun = nil
			got.CommitStatus = nil
			got.Review = nil
			got.Lifecycle = nil
			if !reflect.DeepEqual(got, want) {
				t.Errorf("stored\n%+v\nwant\n%+v", got, want)
			}
		})
	}
}

func TestDeliveriesPythonRefusesAreRefusedForTheSameReason(t *testing.T) {
	reasons := map[string]error{
		"unsupported_event":  ErrUnsupportedEvent,
		"missing_repository": ErrMissingRepository,
		"payload_validation": ErrInvalidPayload,
	}
	vectors := loadVectors(t)
	if len(vectors.RejectedDeliveries) == 0 {
		t.Fatal("no vectors")
	}
	for _, vector := range vectors.RejectedDeliveries {
		t.Run(vector.Name, func(t *testing.T) {
			want, ok := reasons[vector.Reason]
			if !ok {
				t.Fatalf("reason %q is not one of the refusals", vector.Reason)
			}
			if _, err := ParseDelivery(fixture.AppID, vector.Event, vector.DeliveryID, []byte(vector.Raw)); !errors.Is(err, want) {
				t.Errorf("error = %v, want %v", err, want)
			}
		})
	}
}

func TestWebhookCheckRunObservationPreservesAppIDAndNativeStatus(t *testing.T) {
	raw := []byte(`{"action":"created","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"check_run":{"id":42,"head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"build","status":"completed","conclusion":"success","app":{"id":812,"slug":"ci","name":"CI"}}}`)
	ingest, err := ParseDelivery(fixture.AppID, string(model.EventCheckRun), uuid.New(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if ingest.Action == nil || *ingest.Action != "created" || ingest.CheckRun == nil || ingest.CommitStatus != nil {
		t.Fatalf("ingest = %+v, want raw creation activity and a native check-run fact", ingest)
	}
	if ingest.CheckRun.RepositoryID != 100 || ingest.CheckRun.HeadSHA != fixture.SHA || ingest.CheckRun.ID != 42 ||
		ingest.CheckRun.AppID != 812 || ingest.CheckRun.Status != model.CheckRunCompleted ||
		ingest.CheckRun.Conclusion == nil || *ingest.CheckRun.Conclusion != "success" {
		t.Errorf("check run = %+v, want repository 100, app 812 and completed success", ingest.CheckRun)
	}
}

func TestWebhookCommitStatusObservationKeepsLegacyErrorState(t *testing.T) {
	raw := []byte(`{"installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","id":77,"state":"error","context":"continuous-integration/legacy","target_url":"https://ci.test/legacy","updated_at":"2026-10-27T01:06:07Z"}`)
	ingest, err := ParseDelivery(fixture.AppID, string(model.EventStatus), uuid.New(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if ingest.CheckRun != nil || ingest.CommitStatus == nil {
		t.Fatalf("ingest = %+v, want a native commit-status fact", ingest)
	}
	if ingest.CommitStatus.RepositoryID != 100 || ingest.CommitStatus.SHA != fixture.SHA || ingest.CommitStatus.ID != 77 ||
		ingest.CommitStatus.State != model.CommitStatusError || ingest.CommitStatus.Context != "continuous-integration/legacy" ||
		ingest.CommitStatus.TargetURL != "https://ci.test/legacy" || ingest.CommitStatus.UpdatedAt == nil {
		t.Errorf("commit status = %+v, want preserved legacy error state and details", ingest.CommitStatus)
	}
}

func TestMinimalCIWebhooksRemainRawWithoutClaimingCoverage(t *testing.T) {
	tests := []struct {
		name  string
		event string
		raw   []byte
	}{
		{
			name:  "check run",
			event: string(model.EventCheckRun),
			raw:   []byte(`{"action":"created","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"check_run":{"head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}`),
		},
		{
			name:  "commit status",
			event: string(model.EventStatus),
			raw:   []byte(`{"installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}`),
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			ingest, err := ParseDelivery(fixture.AppID, test.event, uuid.New(), test.raw)
			if err != nil {
				t.Fatal(err)
			}
			if ingest.CheckRun != nil || ingest.CommitStatus != nil {
				t.Errorf("ingest claims coverage from incomplete webhook: %+v", ingest)
			}
			sameJSON(t, ingest.Payload, test.raw)
		})
	}
}

func TestMalformedCompleteCIWebhookFactsAreRejected(t *testing.T) {
	tests := []struct {
		name  string
		event string
		raw   string
	}{
		{
			name:  "check run has invalid status",
			event: string(model.EventCheckRun),
			raw: `{"action":"completed","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},` +
				`"check_run":{"id":42,"head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"build","status":"unknown"}}`,
		},
		{
			name:  "status has invalid state",
			event: string(model.EventStatus),
			raw:   `{"installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","id":77,"state":"unknown","context":"ci"}`,
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			_, err := ParseDelivery(fixture.AppID, test.event, uuid.New(), []byte(test.raw))
			if !errors.Is(err, ErrInvalidPayload) {
				t.Errorf("ParseDelivery error = %v, want ErrInvalidPayload", err)
			}
		})
	}
}

func TestReviewBodyEditsRetainTheirRawActionAndPayload(t *testing.T) {
	raw := []byte(`{"action":"edited","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"pull_request":{"number":7,"head":{"sha":"bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"}},"review":{"id":31,"user":{"id":101,"login":"reviewer"},"state":"approved","commit_id":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","body":"revised body"}}`)
	ingest, err := ParseDelivery(fixture.AppID, "pull_request_review", uuid.New(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if ingest.Action == nil || *ingest.Action != "edited" || ingest.Review != nil || ingest.Lifecycle != nil {
		t.Fatalf("ingest = %+v, want raw edited review activity", ingest)
	}
	sameJSON(t, ingest.Payload, raw)
}
