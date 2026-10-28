package store

import (
	"encoding/json"
	"fmt"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/migrations/backfill"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"

	"github.com/google/uuid"
)

// These codecs are test-only fixtures for JSON written by the retired Python/legacy Go storage paths. Current
// service code stores relational identities and typed subscription creation instead.
func marshalHistoricalEventIdentity(event model.EventIdentity) ([]byte, error) {
	switch event := event.(type) {
	case model.ActionsEvent:
		return json.Marshal(struct {
			Provider  string    `json:"provider"`
			RequestID uuid.UUID `json:"request_id"`
			Sequence  int64     `json:"sequence"`
		}{"actions", event.RequestID, event.Sequence})
	case model.GitHubEvent:
		return json.Marshal(struct {
			Provider     string          `json:"provider"`
			AppID        int64           `json:"app_id"`
			DeliveryID   uuid.UUID       `json:"delivery_id"`
			RepositoryID int64           `json:"repository_id"`
			Event        model.EventName `json:"event"`
			Action       *string         `json:"action"`
		}{"github", event.AppID, event.DeliveryID, event.RepositoryID, event.Event, event.Action})
	case model.GitHubObservationEvent:
		return json.Marshal(struct {
			Provider     string          `json:"provider"`
			AppID        int64           `json:"app_id"`
			RepositoryID int64           `json:"repository_id"`
			Event        model.EventName `json:"event"`
			Sequence     int64           `json:"sequence"`
		}{"github_observation", event.AppID, event.RepositoryID, event.Event, event.Sequence})
	case model.GitHubMergeabilityEvent:
		return json.Marshal(struct {
			Provider          string `json:"provider"`
			AppID             int64  `json:"app_id"`
			RepositoryID      int64  `json:"repository_id"`
			PullRequestNumber int64  `json:"pull_request_number"`
			Sequence          int64  `json:"sequence"`
		}{"github_mergeability", event.AppID, event.RepositoryID, event.PullRequestNumber, event.Sequence})
	case model.GitHubMergeabilitySnapshotEvent:
		return json.Marshal(struct {
			Provider          string `json:"provider"`
			AppID             int64  `json:"app_id"`
			RepositoryID      int64  `json:"repository_id"`
			PullRequestNumber int64  `json:"pull_request_number"`
			SnapshotVersion   int64  `json:"snapshot_version"`
		}{"github_mergeability_snapshot", event.AppID, event.RepositoryID, event.PullRequestNumber, event.Version})
	case model.MessageEvent:
		return json.Marshal(struct {
			Provider  string               `json:"provider"`
			MessageID uuid.UUID            `json:"message_id"`
			Sender    model.ServiceAccount `json:"sender"`
		}{"message", event.MessageID, event.Sender})
	default:
		return nil, fmt.Errorf("unsupported historical event identity %T", event)
	}
}

func TestHistoricalGitHubObservationIdentityRemainsReadable(t *testing.T) {
	const wire = `{"provider":"github_observation","app_id":11,"repository_id":22,"event":"check_run","sequence":3}`
	got, err := backfill.DecodeHistoricalEventIdentity([]byte(wire))
	if err != nil {
		t.Fatal(err)
	}
	want := model.GitHubObservationEvent{AppID: 11, RepositoryID: 22, Event: model.EventCheckRun, Sequence: 3}
	if got != want {
		t.Fatalf("decoded historical identity = %#v, want %#v", got, want)
	}
	encoded, err := marshalHistoricalEventIdentity(got)
	if err != nil {
		t.Fatal(err)
	}
	sameJSON(t, encoded, []byte(wire))
}
