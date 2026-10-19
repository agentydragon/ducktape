package runner

import (
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"sort"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

// SessionSnapshot contains the session-owned values adapters need to construct native requests.
// The host refreshes it from the durable Session record whenever it is called.
type SessionSnapshot struct {
	Directory                string
	Cwd                      string
	Model                    string
	ReasoningEffort          string
	Instructions             string
	NativeSessionID          string
	TotalContextBudgetTokens *int
	Resuming                 bool
	ActiveTurnID             string
	HarnessEnvironment       map[string]string
}

// LaunchConfig contains the executable and credentials for one native harness.
type LaunchConfig struct {
	Binary  string
	BaseURL string
	Token   string
}

// NativeReceipt is a raw frame already committed to the Session journal.
type NativeReceipt struct {
	Frame    json.RawMessage
	Sequence uint64
}

// UserMessageConfirmation records native evidence that an input reached a model request.
type UserMessageConfirmation struct {
	HarnessMessageID string
	Text             string
	OriginCommandIDs []string
	TurnID           string
}

// AdapterJournal exposes the durable evidence needed to translate frames and reconcile a turn.
type AdapterJournal interface {
	ObservedItems(context.Context, string) (map[string]ObservedItem, error)
	TurnEvents(context.Context, string) ([]*eventlogpb.EventEntry, error)
	RememberAdapterItem(context.Context, string, string) (bool, error)
	HasAdapterItem(context.Context, string, string) (bool, error)
	NextAdapterBlock(context.Context, string, string) (int, error)
}

// AdapterHost is the Session boundary used by native adapters. Native frames remain raw JSON at
// this boundary so an adapter can ignore future fields without rewriting their archived evidence.
type AdapterHost interface {
	Session() SessionSnapshot
	Journal() AdapterJournal
	SendNative(context.Context, json.RawMessage) error
	RequestNative(context.Context, json.RawMessage, func(json.RawMessage) bool) (NativeReceipt, error)
	Emit(context.Context, *eventpb.Event, []uint64) error
	ConfirmUserMessage(context.Context, UserMessageConfirmation, []uint64) error
	ModelChanged(context.Context, string, string, []uint64) error
	ReasoningEffortChanged(context.Context, string, string, []uint64) error
	FailCommand(context.Context, string, string, []uint64) error
	NoopCommand(context.Context, string, string, []uint64) error
}

// Adapter launches one native harness and translates its raw frames into durable observations.
type Adapter interface {
	Command() ([]string, error)
	Environment() (map[string]string, error)
	Handshake(context.Context) (string, error)
	Reconcile(context.Context, string, bool) (*eventpb.ConversationReconciled, error)
	Submit(context.Context, string, string) error
	Interrupt(context.Context, string) error
	ChangeModel(context.Context, string, string) error
	ChangeReasoningEffort(context.Context, string, string) error
	OnFrame(context.Context, json.RawMessage, uint64) error
}

func newUUID() (string, error) {
	var raw [16]byte
	if _, err := rand.Read(raw[:]); err != nil {
		return "", err
	}
	raw[6] = raw[6]&0x0f | 0x40
	raw[8] = raw[8]&0x3f | 0x80
	encoded := hex.EncodeToString(raw[:])
	return fmt.Sprintf("%s-%s-%s-%s-%s", encoded[:8], encoded[8:12], encoded[12:16], encoded[16:20], encoded[20:]), nil
}

func marshalNative(value any) (json.RawMessage, error) {
	encoded, err := json.Marshal(value)
	return json.RawMessage(encoded), err
}

func ensureDirectory(path string) error {
	return os.MkdirAll(path, 0o700)
}

func copyEnvironment(source map[string]string) map[string]string {
	result := make(map[string]string, len(source)+5)
	for key, value := range source {
		result[key] = value
	}
	return result
}

func sortedObservedItemIDs(items map[string]ObservedItem) []string {
	ids := make([]string, 0, len(items))
	for id := range items {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	return ids
}
