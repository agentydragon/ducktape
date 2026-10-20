package store

import (
	"bytes"
	_ "embed"
	"encoding/hex"
	"encoding/json"
	"reflect"
	"testing"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
)

// golden.json holds what the Python service writes and accepts: the destination key of an inbox, the creation
// JSON of a subscription and the event JSON of an entry, each from the Python code that produces it, with the
// creations Python's models refuse and the pairs it tells apart. It also holds the webhook deliveries Python
// accepts and refuses, with what its ingest stores for each, and the key it indexes each subject by.
// `generate_golden.py` writes it and `test_golden.py` checks it is current.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
	DestinationKeys []struct {
		Name           string         `json:"name"`
		DestinationRef DestinationRef `json:"destination_ref"`
		SessionID      string         `json:"session_id"`
		Key            string         `json:"key"`
	} `json:"destination_keys"`
	// Subscriptions are valid creations: Input is what a caller sends, Stored the JSON Python writes for it.
	Subscriptions []struct {
		Name   string          `json:"name"`
		Input  json.RawMessage `json:"input"`
		Stored json.RawMessage `json:"stored"`
	} `json:"subscriptions"`
	InvalidSubscriptions []struct {
		Name  string          `json:"name"`
		Input json.RawMessage `json:"input"`
	} `json:"invalid_subscriptions"`
	// Comparisons are pairs of creations and whether Python's idempotency check calls them the same.
	Comparisons []struct {
		Name string          `json:"name"`
		A    json.RawMessage `json:"a"`
		B    json.RawMessage `json:"b"`
		Same bool            `json:"same"`
	} `json:"comparisons"`
	Events []struct {
		Name  string          `json:"name"`
		Event json.RawMessage `json:"event"`
	} `json:"events"`
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
	// SubjectKeys are subjects with the kind and key Python indexes them by.
	SubjectKeys []struct {
		Subject json.RawMessage `json:"subject"`
		Kind    string          `json:"kind"`
		Key     string          `json:"key"`
	} `json:"subject_keys"`
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
		ingest.Repository = &GitHubRepository{ID: *d.Ingest.RepositoryID, Name: *d.Ingest.RepositoryName}
	}
	for _, subject := range d.Ingest.Subjects {
		ingest.Subjects = append(ingest.Subjects, GitHubSubjectKey{RepositoryID: subject.RepositoryID, Kind: subject.Kind, Key: subject.SubjectKey})
	}
	for _, revision := range d.Ingest.Revisions {
		ingest.Revisions = append(ingest.Revisions, GitHubHeadRevision{
			Repository: GitHubRepository{ID: revision.RepositoryID, Name: revision.RepositoryName}, SHA: revision.SHA,
		})
	}
	return ingest
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := decode(goldenJSON, &vectors); err != nil {
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
