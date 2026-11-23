package harness

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net/url"

	eventpb "github.com/agentydragon/ducktape/agentplane/proto/event"
	eventlogpb "github.com/agentydragon/ducktape/agentplane/proto/event_log"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/reflect/protoreflect"
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
	// Environment holds the variables only this Session's tools see, beside the Runtime-wide harness
	// environment: its Session ID. A shared process applies them per native thread.
	Environment map[string]string
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

// ErrUnknownRecoveryTurn means no TurnStarted has been journaled under this native turn ID yet.
var ErrUnknownRecoveryTurn = errors.New("unknown recovery turn")

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

// UserMessageConfirmation records native evidence that an input entered the harness conversation.
type UserMessageConfirmation struct {
	HarnessMessageID string
	Text             string
	OriginCommandIDs []string
	TurnID           string
}

// AdapterJournal exposes the durable evidence needed to translate frames and reconcile a turn.
type AdapterJournal interface {
	ObservedItems(context.Context, string) (map[string]ObservedItem, error)
	RecoveryTurns(context.Context) ([]string, error)
	HasTurn(context.Context, string) (bool, error)
	TurnEvents(context.Context, string) ([]*eventlogpb.EventEntry, error)
	PendingCommandRecords(context.Context) ([]CommandRecord, error)
	PendingNativeRequests(context.Context) ([]NativeRequest, error)
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
	RecordNativeRequest(context.Context, string, NativeRequestKind, []string) error
	SendNative(context.Context, json.RawMessage) error
	// RequestNativeHandshake sequences startup and read-only history exchanges outside the stdout
	// reader; command methods should send and
	// let OnFrame advance their durable state instead of waiting for a response.
	RequestNativeHandshake(context.Context, json.RawMessage, func(json.RawMessage) bool) (NativeReceipt, error)
	Emit(context.Context, *eventpb.Event, []uint64) error
	ConfirmUserMessage(context.Context, UserMessageConfirmation, []uint64) error
	ModelChanged(context.Context, string, string, []uint64) error
	ReasoningEffortChanged(context.Context, string, string, []uint64) error
	FailCommand(context.Context, string, string, []uint64) error
	NoopCommand(context.Context, string, string, []uint64) error
}

// Observation wraps one generated observation in the Event vocabulary. The generated
// descriptor owns the mapping, avoiding a second list of observation field names. It panics
// on a message that is not one of the Event observations.
func Observation(value proto.Message) *eventpb.Event {
	e := &eventpb.Event{}
	d := e.ProtoReflect().Descriptor().Fields()
	for i := 0; i < d.Len(); i++ {
		f := d.Get(i)
		if f.ContainingOneof() != nil && f.Message() != nil && f.Message().FullName() == value.ProtoReflect().Descriptor().FullName() {
			e.ProtoReflect().Set(f, protoreflect.ValueOfMessage(value.ProtoReflect()))
			return e
		}
	}
	panic(fmt.Sprintf("%T is not an Environment Runtime observation", value))
}

// Adapter prepares one Session's native conversation and translates its raw frames into durable
// observations.
type Adapter interface {
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

// ProcessAdapter is an Adapter whose Session runs its own native harness process.
type ProcessAdapter interface {
	Adapter
	Command() []string
	// Environment is the harness's own variables; the Runtime adds them to the Runtime-wide harness
	// environment and the Session's Environment.
	Environment() (map[string]string, error)
}

type RecoveryAdapter interface {
	Recover(context.Context) error
}

// InputRecoveryAdapter consults the live native history after the handshake.
// It must not replay inputs whose native outcome is uncertain.
type InputRecoveryAdapter interface {
	RecoverInputs(context.Context) error
	NeedsInputRecovery() bool
}
