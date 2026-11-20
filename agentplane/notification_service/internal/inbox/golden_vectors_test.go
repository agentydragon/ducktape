package inbox

import (
	_ "embed"
	"encoding/json"
	"testing"

	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"github.com/agentydragon/ducktape/agentplane/testing/jsonassert"
	"github.com/google/uuid"
	"google.golang.org/protobuf/encoding/protojson"
)

// golden.json holds persisted payload and runner-byte vectors shared with older writers. The
// retired Python service and fixture generator are in git history at commit 180ae8a62d.
//
//go:embed golden.json
var goldenJSON []byte

type golden struct {
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
}

func loadGolden(t *testing.T) golden {
	t.Helper()
	var vectors golden
	if err := json.Unmarshal(goldenJSON, &vectors); err != nil {
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

// sameJSON compares JSONB values without considering object key order or whitespace.
func sameJSON(t *testing.T, got, want []byte) {
	t.Helper()
	if !jsonassert.Equal(t, got, want) {
		t.Errorf("got  %s\nwant %s", got, want)
	}
}
