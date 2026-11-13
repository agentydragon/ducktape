package store

import (
	"bytes"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"testing"

	"github.com/agentydragon/ducktape/agentplane/notification_service/internal/storagecodec"

	"github.com/agentydragon/ducktape/agentplane/notification_service/model"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
)

// golden.json holds persisted destination keys and payload and runner-byte vectors shared with older writers. The
// retired Python service and fixture generator are in git history at commit 180ae8a62d.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
	DestinationKeys []struct {
		Name           string               `json:"name"`
		DestinationRef model.DestinationRef `json:"destination_ref"`
		SessionID      string               `json:"session_id"`
		Key            string               `json:"key"`
	} `json:"destination_keys"`
	// ActionEvents are Action events as the Action Service sends them, with the payload Python stores for each.
	ActionEvents []struct {
		Name    string          `json:"name"`
		Wire    json.RawMessage `json:"wire"`
		Payload json.RawMessage `json:"payload"`
	} `json:"action_events"`
	// InvalidActionEvents are events Python refuses to read.
	InvalidActionEvents []struct {
		Name string          `json:"name"`
		Wire json.RawMessage `json:"wire"`
	} `json:"invalid_action_events"`
	// Notices are the command texts Python prepares for the cursors an inbox has at preparation.
	Notices []struct {
		Name         string    `json:"name"`
		InboxID      uuid.UUID `json:"inbox_id"`
		Acknowledged int64     `json:"acknowledged"`
		Through      int64     `json:"through"`
		Text         string    `json:"text"`
	} `json:"notices"`
	// CommandID is a notice's command ID as a runner's receipts name it: Python's `str(UUID)`, whatever the case
	// the ID was parsed from.
	CommandID struct {
		Input   string `json:"input"`
		Written string `json:"written"`
	} `json:"command_id"`
	// RunnerEntries are History entries as protobuf JSON, with the bytes Python's deterministic serialization
	// of each gives, which is what `notice.runner_entry` keeps (hex).
	RunnerEntries []struct {
		Name       string          `json:"name"`
		Entry      json.RawMessage `json:"entry"`
		Serialized string          `json:"serialized"`
	} `json:"runner_entries"`
	// Deliveries are signed webhook bodies Python accepts, with the call its ingest makes for each.
	Deliveries []goldenDelivery `json:"deliveries"`
	// RejectedDeliveries are bodies Python refuses, with why.
	RejectedDeliveries []struct {
		Name       string    `json:"name"`
		Event      string    `json:"event"`
		DeliveryID uuid.UUID `json:"delivery_id"`
		Raw        string    `json:"raw"`
		Reason     string    `json:"reason"`
	} `json:"rejected_deliveries"`
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

// ingest is what Python's ingest stored for the delivery, as the store takes it.
func (d goldenDelivery) ingest(t *testing.T) GitHubIngest {
	t.Helper()
	digest, err := hex.DecodeString(d.Ingest.Digest)
	if err != nil {
		t.Fatal(err)
	}
	ingest := GitHubIngest{
		AppID: d.Ingest.AppID, DeliveryID: d.Ingest.DeliveryID, InstallationID: d.Ingest.InstallationID,
		Event: d.Ingest.Event, Digest: digest, Payload: d.Ingest.Payload, Action: d.Ingest.Action, HeadSHA: d.Ingest.HeadSHA,
	}
	if d.Ingest.RepositoryID != nil {
		ingest.Repository = &model.GitHubRepository{ID: *d.Ingest.RepositoryID, Name: *d.Ingest.RepositoryName}
	}
	for _, subject := range d.Ingest.Subjects {
		ingest.Subjects = append(ingest.Subjects, GitHubSubjectKey{RepositoryID: subject.RepositoryID, Kind: subject.Kind, Key: subject.SubjectKey})
	}
	for _, revision := range d.Ingest.Revisions {
		ingest.Revisions = append(ingest.Revisions, model.GitHubHeadRevision{
			Repository: model.GitHubRepository{ID: revision.RepositoryID, Name: revision.RepositoryName}, SHA: revision.SHA,
		})
	}
	return ingest
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := storagecodec.Decode(goldenJSON, &vectors); err != nil {
		t.Fatal(err)
	}
	return vectors
}

// runnerEntry reads a History entry from its protobuf JSON.
func runnerEntry(t *testing.T, data json.RawMessage) *eventlogpb.EventEntry {
	t.Helper()
	var entry eventlogpb.EventEntry
	if err := protojson.Unmarshal(data, &entry); err != nil {
		t.Fatalf("%s: %v", data, err)
	}
	return &entry
}

// sameJSON compares two JSON documents as values: key order and whitespace are not part of what a JSONB column
// keeps.
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
		t.Errorf("got  %s\nwant %s", got, want)
	}
}
