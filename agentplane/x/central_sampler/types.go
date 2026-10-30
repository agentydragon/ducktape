package centralsampler

import (
	"database/sql/driver"
	"encoding/json"
	"errors"
	"fmt"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	samplerpb "github.com/agentydragon/ducktape/agentplane/x/central_sampler/samplerpb"
)

// Owner fences every runtime write to the lease generation that acquired it.
type Owner struct {
	ConversationID string
	Generation     int
	Token          string
}

// Conversation is the durable provider conversation and its current execution fence.
type Conversation struct {
	ID              string
	Generation      int
	Owner           *string
	LeaseUntil      *time.Time
	Items           []ResponsesItem
	Pending         *PendingIntent
	Stopped         bool
	ExecutorSession *string
	ContextItems    []ResponsesItem
	Instructions    *string
	ContextTokens   *int
	// ExecutorShell is the shell name the bound exec-server last reported.
	ExecutorShell *string
	// EnvironmentContext is the last model-visible environment context.
	EnvironmentContext *EnvironmentContext
}

func (c *Conversation) Context() []ResponsesItem {
	if c.ContextItems == nil {
		return c.Items
	}
	return c.ContextItems
}

// IntentKind is the kind of any recorded intent: a pending one or a nested effect inside it.
type IntentKind string

const (
	IntentCell             IntentKind = "cell"
	IntentSampling         IntentKind = "sampling"
	IntentCompaction       IntentKind = "compaction"
	IntentInterrupt        IntentKind = "interrupt"
	IntentTool             IntentKind = "tool"
	IntentExecutor         IntentKind = "executor"
	IntentInterruptProcess IntentKind = "interrupt_process"
)

func (kind IntentKind) valid() bool {
	switch kind {
	case IntentCell, IntentSampling, IntentCompaction, IntentInterrupt, IntentTool, IntentExecutor, IntentInterruptProcess:
		return true
	default:
		return false
	}
}

func (kind *IntentKind) UnmarshalJSON(data []byte) error {
	var value string
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	candidate := IntentKind(value)
	if !candidate.valid() {
		return fmt.Errorf("unknown intent kind %q", value)
	}
	*kind = candidate
	return nil
}

// PendingIntentKind is the subset of IntentKind that can be a conversation's pending intent.
type PendingIntentKind string

const (
	PendingIntentCell       = PendingIntentKind(IntentCell)
	PendingIntentSampling   = PendingIntentKind(IntentSampling)
	PendingIntentCompaction = PendingIntentKind(IntentCompaction)
	PendingIntentInterrupt  = PendingIntentKind(IntentInterrupt)
)

func (kind PendingIntentKind) valid() bool {
	switch kind {
	case PendingIntentCell, PendingIntentSampling, PendingIntentCompaction, PendingIntentInterrupt:
		return true
	default:
		return false
	}
}

func (kind *PendingIntentKind) UnmarshalJSON(data []byte) error {
	var value string
	if err := json.Unmarshal(data, &value); err != nil {
		return err
	}
	candidate := PendingIntentKind(value)
	if !candidate.valid() {
		return fmt.Errorf("unknown pending intent kind %q", value)
	}
	*kind = candidate
	return nil
}

type PendingIntent struct {
	ID   string            `json:"id"`
	Kind PendingIntentKind `json:"kind"`
}

func (pending PendingIntent) Validate() error {
	if pending.ID == "" || !pending.Kind.valid() {
		return errors.New("pending intent requires a known kind and ID")
	}
	return nil
}

type SessionAttribution struct {
	SessionID string
	ThreadID  string
	TurnID    string
}

// Session stores the immutable launch request plus the event and command cursors.
type Session struct {
	ID                string
	ConversationID    string
	AccountUID        string
	Configuration     []byte
	Model             string
	Effort            string
	SourceID          string
	Cursor            uint64
	ActiveTurn        *string
	ActiveCommand     *string
	WorkspacePrepared bool
	SetupState        runnerpb.SetupState
	SetupStarted      bool
	Blocked           bool
	BlockedReason     *string
}

// IntentRequest is the stored request of a pending intent. Nested effects (RecordNested) keep
// opaque protocol payloads and are not described by it.
type IntentRequest struct {
	Kind PendingIntentKind `json:"kind"`
	// Call is set for cell intents.
	Call *ResponsesToolCall `json:"call,omitempty"`
	// Dispatched is false for a cell intent recorded only to reject its call.
	Dispatched *bool `json:"dispatched,omitempty"`
	// Body is the provider request of a sampling or compaction intent.
	Body *ResponsesRequest `json:"body,omitempty"`
}

// IntentResultStatus is the "status" of an intent result the sampler itself writes. Nested
// effects store remote-reported results that may carry other statuses.
type IntentResultStatus string

const (
	IntentResultRejected  IntentResultStatus = "rejected"
	IntentResultAbandoned IntentResultStatus = "abandoned"
)

// RejectedCellResult is the stored result of a cell intent rejected before dispatch.
type RejectedCellResult struct {
	Status     IntentResultStatus `json:"status"`
	Dispatched *bool              `json:"dispatched"`
	Reason     string             `json:"reason"`
}

// Intent is a committed remote-effect request and its acknowledged result, if known.
type Intent struct {
	ID             string
	ConversationID string
	Generation     int
	Kind           IntentKind
	// CallID is the model call a cell intent serves; empty for other intents.
	CallID string
	// ResultWire is the raw JSON result; nil while unresolved.
	ResultWire []byte
}

// TurnResult is how a turn step or a whole command ended.
type TurnResult string

const (
	// Terminal results end a command.
	TurnDone           TurnResult = "done"
	TurnInterrupted    TurnResult = "interrupted"
	TurnEffectsUnknown TurnResult = "effects_unknown"
	TurnCompact        TurnResult = "compact"
	// TurnFailed only labels the turn result of interrupts settled by a failed execution.
	TurnFailed TurnResult = "failed"
	// Continuing results make the turn loop advance again.
	TurnCell         TurnResult = "cell"
	TurnSample       TurnResult = "sample"
	TurnNotification TurnResult = "notification"
	TurnSteer        TurnResult = "steer"
)

// Outcome is the command outcome a finished command records.
func (result TurnResult) Outcome() samplerpb.CommandOutcome {
	if result == TurnEffectsUnknown {
		return samplerpb.CommandOutcome_COMMAND_OUTCOME_UNKNOWN
	}
	return samplerpb.CommandOutcome_COMMAND_OUTCOME_APPLIED
}

// endsTurn reports whether the turn loop stops advancing.
func (result TurnResult) endsTurn() bool {
	return result == TurnDone || result == TurnInterrupted || result == TurnEffectsUnknown
}

// finishesCommand reports whether a command may be finished with this result; a compact command
// finishes with TurnCompact.
func (result TurnResult) finishesCommand() bool {
	return result.endsTurn() || result == TurnCompact
}

// ProcessStatus is the lifecycle state of a RemoteProcess.
type ProcessStatus string

const (
	ProcessStarting ProcessStatus = "starting"
	ProcessRunning  ProcessStatus = "running"
	// ProcessExited has been fully consumed; ProcessExitedUnread still holds unread output.
	ProcessExited       ProcessStatus = "exited"
	ProcessExitedUnread ProcessStatus = "exited_unread"
	ProcessAbandoned    ProcessStatus = "abandoned"
	// ProcessUnknown is a process whose remote outcome could not be determined.
	ProcessUnknown ProcessStatus = "unknown"
)

func (status ProcessStatus) valid() bool {
	switch status {
	case ProcessStarting, ProcessRunning, ProcessExited, ProcessExitedUnread, ProcessAbandoned, ProcessUnknown:
		return true
	default:
		return false
	}
}

// Scan rejects stored statuses the sampler does not know.
func (status *ProcessStatus) Scan(src any) error {
	var value string
	switch typed := src.(type) {
	case string:
		value = typed
	case []byte:
		value = string(typed)
	default:
		return fmt.Errorf("process status has type %T, expected string", src)
	}
	candidate := ProcessStatus(value)
	if !candidate.valid() {
		return fmt.Errorf("unknown process status %q", value)
	}
	*status = candidate
	return nil
}

// Value rejects statuses the sampler does not know, so an unknown one never reaches the database.
func (status ProcessStatus) Value() (driver.Value, error) {
	if !status.valid() {
		return nil, fmt.Errorf("unknown process status %q", string(status))
	}
	return string(status), nil
}

// CellStatus is the lifecycle state of a LiveCell row.
type CellStatus string

const (
	CellLive       CellStatus = "live"
	CellCompleted  CellStatus = "completed"
	CellTerminated CellStatus = "terminated"
	CellAbandoned  CellStatus = "abandoned"
)

func (status CellStatus) valid() bool {
	switch status {
	case CellLive, CellCompleted, CellTerminated, CellAbandoned:
		return true
	default:
		return false
	}
}

// Scan rejects stored statuses the sampler does not know.
func (status *CellStatus) Scan(src any) error {
	var value string
	switch typed := src.(type) {
	case string:
		value = typed
	case []byte:
		value = string(typed)
	default:
		return fmt.Errorf("cell status has type %T, expected string", src)
	}
	candidate := CellStatus(value)
	if !candidate.valid() {
		return fmt.Errorf("unknown cell status %q", value)
	}
	*status = candidate
	return nil
}

// Value rejects statuses the sampler does not know, so an unknown one never reaches the database.
func (status CellStatus) Value() (driver.Value, error) {
	if !status.valid() {
		return nil, fmt.Errorf("unknown cell status %q", string(status))
	}
	return string(status), nil
}

// Command is an admitted session command and its durable execution status.
type Command struct {
	SessionID     string
	ID            string
	Payload       []byte
	Admission     uint64
	Outcome       samplerpb.CommandOutcome
	FailureReason *string
}

// RemoteProcess retains unread output until the model consumes it.
type RemoteProcess struct {
	ID             string
	ConversationID string
	Generation     int
	AfterSequence  *uint64
	Status         ProcessStatus
	TTY            bool
	ExitCode       *int
	// BufferedOutput is the retained head and tail of unread output; OmittedBytes were dropped
	// between them.
	BufferedOutput []byte
	OmittedBytes   int
	// ModelSessionID is the unified exec session number the model uses; absent for setup.
	ModelSessionID *int
}

// LiveCell is a JavaScript cell whose result has not yet been durably recorded.
type LiveCell struct {
	ID             string
	ConversationID string
	Generation     int
	Status         CellStatus
}

// ExecutionState is the set of remote handles whose effects may still be live.
type ExecutionState struct {
	Cells     []LiveCell
	Processes []RemoteProcess
}

// InterruptRequest is scoped to one lease generation and, for session commands, one turn.
type InterruptRequest struct {
	ID             string
	ConversationID string
	Generation     int
	TargetTurn     *string
	Result         map[string]any
}

// Errors are distinguished by callers at the service boundary.
var (
	ErrConflict     = errors.New("resource conflicts with an existing request")
	ErrMissing      = errors.New("resource not found")
	ErrForbidden    = errors.New("resource belongs to another account")
	ErrPrecondition = errors.New("resource is not in a state that permits this operation")
	ErrInvalid      = errors.New("request is invalid")
)

type LostOwnershipError struct{ Reason string }

func (e *LostOwnershipError) Error() string {
	if e.Reason == "" {
		return "sampler generation is stale or expired"
	}
	return e.Reason
}

type UnknownEffectError struct{ Reason string }

func (e *UnknownEffectError) Error() string {
	if e.Reason == "" {
		return "remote effect outcome is unknown; automatic replay is unsafe"
	}
	return e.Reason
}

type InterruptPendingError struct{}

func (InterruptPendingError) Error() string {
	return "an interrupt is pending; the next dispatch is blocked"
}

// KnownFailureError records a provider or pre-dispatch rejection whose effect is known.
type KnownFailureError struct {
	Evidence map[string]any
	Message  string
}

func (e *KnownFailureError) Error() string {
	if e.Message != "" {
		return e.Message
	}
	return fmt.Sprint(e.Evidence)
}
