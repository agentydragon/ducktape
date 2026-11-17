package github

import (
	"encoding/hex"
	"errors"
	"reflect"
	"testing"

	githubmodel "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/model" // Distinguishes GitHub-owned types from common notification model types.

	githubstate "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/state" // Distinguishes GitHub persistence from local state values.
	fixture "github.com/agentydragon/ducktape/agentplane/notification_service/internal/sources/github/testing"   // Shared fake API server; avoids colliding with Go testing.
	"github.com/agentydragon/ducktape/agentplane/testing/jsonassert"

	"github.com/google/uuid"
)

var notificationGoldenRlocation string

// sameJSON compares two documents as values, for what a JSONB column keeps: not key order, whitespace or
// duplicate keys.
func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	if !jsonassert.Equal(t, got, want) {
		t.Errorf("payload %s, want %s", got, want)
	}
}

func TestJSONAssertionValueSemantics(t *testing.T) {
	compare := func(got, want string, equal bool) {
		t.Helper()
		if actual := jsonassert.Equal(t, []byte(got), []byte(want)); actual != equal {
			t.Errorf("Equal(%s, %s) = %t, want %t", got, want, actual, equal)
		}
	}
	compare(`{"a": 9007199254740993, "duplicate": 1, "duplicate": 2}`, `{"duplicate":2,"a":9007199254740993}`, true)
	compare(`{"id":9007199254740993}`, `{"id":9007199254740992}`, false)
	compare(`{"number":1}`, `{"number":1.0}`, false)
}

func TestDeliveriesPythonAcceptsAreStoredAsPythonStoresThem(t *testing.T) {
	vectors := fixture.LoadGolden(t, notificationGoldenRlocation)
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
			want := githubstate.Ingest{
				AppID: vector.Ingest.AppID, DeliveryID: vector.DeliveryID, InstallationID: vector.Ingest.InstallationID,
				Event: vector.Event, Digest: digest, Action: vector.Ingest.Action, HeadSHA: vector.Ingest.HeadSHA,
			}
			if vector.Ingest.RepositoryID != nil {
				want.Repository = &githubmodel.Repository{ID: *vector.Ingest.RepositoryID, Name: *vector.Ingest.RepositoryName}
			}
			for _, subject := range vector.Ingest.Subjects {
				want.Subjects = append(want.Subjects, githubstate.SubjectKey{
					RepositoryID: subject.RepositoryID, Kind: subject.Kind, Key: subject.SubjectKey,
				})
			}
			for _, revision := range vector.Ingest.Revisions {
				want.Revisions = append(want.Revisions, githubmodel.HeadRevision{
					Repository: githubmodel.Repository{ID: revision.RepositoryID, Name: revision.RepositoryName}, SHA: revision.SHA,
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
	vectors := fixture.LoadGolden(t, notificationGoldenRlocation)
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
	ingest, err := ParseDelivery(fixture.AppID, string(githubmodel.EventCheckRun), uuid.New(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if ingest.Action == nil || *ingest.Action != "created" || ingest.CheckRun == nil || ingest.CommitStatus != nil {
		t.Fatalf("ingest = %+v, want raw creation activity and a native check-run fact", ingest)
	}
	if ingest.CheckRun.RepositoryID != 100 || ingest.CheckRun.HeadSHA != fixture.SHA || ingest.CheckRun.ID != 42 ||
		ingest.CheckRun.AppID != 812 || ingest.CheckRun.Status != githubmodel.CheckRunCompleted ||
		ingest.CheckRun.Conclusion == nil || *ingest.CheckRun.Conclusion != "success" {
		t.Errorf("check run = %+v, want repository 100, app 812 and completed success", ingest.CheckRun)
	}
}

func TestWebhookCommitStatusObservationKeepsLegacyErrorState(t *testing.T) {
	raw := []byte(`{"installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","id":77,"state":"error","context":"continuous-integration/legacy","target_url":"https://ci.test/legacy","updated_at":"2026-10-27T01:06:07Z"}`)
	ingest, err := ParseDelivery(fixture.AppID, string(githubmodel.EventStatus), uuid.New(), raw)
	if err != nil {
		t.Fatal(err)
	}
	if ingest.CheckRun != nil || ingest.CommitStatus == nil {
		t.Fatalf("ingest = %+v, want a native commit-status fact", ingest)
	}
	if ingest.CommitStatus.RepositoryID != 100 || ingest.CommitStatus.SHA != fixture.SHA || ingest.CommitStatus.ID != 77 ||
		ingest.CommitStatus.State != githubmodel.CommitStatusError || ingest.CommitStatus.Context != "continuous-integration/legacy" ||
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
			event: string(githubmodel.EventCheckRun),
			raw:   []byte(`{"action":"created","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},"check_run":{"head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}`),
		},
		{
			name:  "commit status",
			event: string(githubmodel.EventStatus),
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
			event: string(githubmodel.EventCheckRun),
			raw: `{"action":"completed","installation":{"id":11},"repository":{"id":100,"full_name":"owner/repo"},` +
				`"check_run":{"id":42,"head_sha":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","name":"build","status":"unknown"}}`,
		},
		{
			name:  "status has invalid state",
			event: string(githubmodel.EventStatus),
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
