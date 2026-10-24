package harness

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
)

// MaxFrameLineBytes bounds native frames and retained transcript lines.
const MaxFrameLineBytes = 64 * 1024 * 1024

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

// PlatformURL is `path` on the origin of BaseURL: a route the LLM ingress serves beside the model API,
// which BaseURL may sit under (Codex's `/v1`).
func (c LaunchConfig) PlatformURL(path string) (*url.URL, error) {
	u, err := url.Parse(c.BaseURL)
	if err != nil || u.Host == "" || (u.Scheme != "http" && u.Scheme != "https") {
		return nil, errors.New("model endpoint must be an absolute HTTP(S) URL")
	}
	u.Path, u.RawPath, u.RawQuery, u.Fragment = path, "", "", ""
	return u, nil
}

// NativeReceipt is a raw frame already committed to the Session journal.
type NativeReceipt struct {
	Frame    json.RawMessage
	Sequence uint64
}

var ErrNativeResponsePending = errors.New("native response is pending")

// NativeResponsePendingError means a native write may have reached the harness, but its
// outcome is uncertain. The command may still take effect; callers must leave it pending.
type NativeResponsePendingError struct{ Cause error }

func (e *NativeResponsePendingError) Error() string {
	if e.Cause == nil {
		return ErrNativeResponsePending.Error()
	}
	return fmt.Sprintf("%s: %v", ErrNativeResponsePending, e.Cause)
}

func (e *NativeResponsePendingError) Unwrap() error { return e.Cause }

func (e *NativeResponsePendingError) Is(target error) bool {
	return target == ErrNativeResponsePending
}

type PendingCommandInfo struct {
	CommandID                  string
	AdmittedCursor             uint64
	SubmitText                 string
	ChangeModelValue           string
	ChangeReasoningEffortValue string
	DispatchPlanned            bool
	NativeCorrelation          *NativeCorrelation
	LegacyNativeCorrelation    json.RawMessage
}

// NativeCorrelation is the versioned adapter payload needed to match a native response to a
// dispatched command. Payload is encoded according to the adapter's own schema.
type NativeCorrelation struct {
	Adapter       string          `json:"adapter"`
	SchemaVersion uint32          `json:"schema_version"`
	Payload       json.RawMessage `json:"payload"`
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
	PendingCommandInfo(context.Context) ([]PendingCommandInfo, error)
	Since(context.Context, uint64, int) ([]*eventlogpb.EventEntry, error)
	LastCursor() uint64
	RememberAdapterItem(context.Context, string, string) (bool, error)
	HasAdapterItem(context.Context, string, string) (bool, error)
	NextAdapterBlock(context.Context, string, string) (int, error)
}

// AdapterHost is the Session boundary used by native adapters. Native frames remain raw JSON at
// this boundary so an adapter can ignore future fields without rewriting their archived evidence.
type AdapterHost interface {
	Session() SessionSnapshot
	Journal() AdapterJournal
	RecordNativeCorrelation(context.Context, string, NativeCorrelation) error
	SendNative(context.Context, json.RawMessage) error
	// RequestNativeHandshake sequences startup protocol exchanges; command methods should send and
	// let OnFrame advance their durable state instead of waiting for a response.
	RequestNativeHandshake(context.Context, json.RawMessage, func(json.RawMessage) bool) (NativeReceipt, error)
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
	// BlocksCommandDispatch reports whether a native response must advance this adapter's
	// current command before the Session dispatches later queued commands.
	BlocksCommandDispatch() bool
	Submit(context.Context, string, string) error
	Interrupt(context.Context, string) error
	ChangeModel(context.Context, string, string) error
	ChangeReasoningEffort(context.Context, string, string) error
	OnFrame(context.Context, json.RawMessage, uint64) error
}

type RecoveryAdapter interface {
	Recover(context.Context) error
}
