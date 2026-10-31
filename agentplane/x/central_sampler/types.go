package centralsampler

import (
	"encoding/json"
	"errors"
	"fmt"
	"time"

	runnerpb "github.com/agentydragon/ducktape/agentplane/proto/runner"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
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
	API             protocol.APIStyle
	Archive         protocol.Context
	Pending         *PendingIntent
	Stopped         bool
	ExecutorSession *string
	ActiveContext   protocol.Context
	Instructions    *string
	ContextTokens   *int
	// ExecutorShell is the shell name the bound exec-server last reported.
	ExecutorShell *string
	// EnvironmentContext is the last model-visible environment context.
	EnvironmentContext *EnvironmentContext
}

func (c *Conversation) Context() protocol.Context {
	if c.ActiveContext == nil {
		return c.Archive
	}
	return c.ActiveContext
}

func (c *Conversation) apply(change func(protocol.Context) error) error {
	if err := change(c.Archive); err != nil {
		return err
	}
	if c.ActiveContext != nil {
		return change(c.ActiveContext)
	}
	return nil
}

func (c *Conversation) appendToolResult(call protocol.ToolInvocation, output []protocol.ContentPart) error {
	// The archive ordinal is authoritative. Compacted active context has different local ordinals.
	original, err := c.Archive.FindCall(call.CallID)
	if err != nil {
		return err
	}
	if original.Encoding != call.Encoding || original.Name != call.Name || original.Input != call.Input ||
		(call.SourceRecord != 0 && original.SourceRecord != call.SourceRecord) {
		return protocol.ErrInvalid
	}
	return c.apply(func(state protocol.Context) error {
		local, err := state.FindCall(call.CallID)
		if err != nil {
			return err
		}
		if local.Encoding != call.Encoding || local.Name != call.Name || local.Input != call.Input {
			return protocol.ErrInvalid
		}
		return state.AppendToolResult(local, output)
	})
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
	Call *protocol.ToolInvocation `json:"call,omitempty"`
	// Dispatched is false for a cell intent recorded only to reject its call.
	Dispatched *bool `json:"dispatched,omitempty"`
	// ProviderRequest is set for sampling and compaction intents; its fields are inlined.
	*ProviderRequest
}

// ProviderRequest records a provider call before dispatch. Authentication is deliberately absent:
// it is committed in the same transaction as the pending intent, before any upstream request.
type ProviderRequest struct {
	API           protocol.APIStyle `json:"api"`
	Connection    string            `json:"connection"`
	ProviderModel string            `json:"provider_model"`
	Method        string            `json:"method"`
	URL           string            `json:"url"`
	Headers       map[string]string `json:"headers"`
	// Body and WireRequest are the same outgoing bytes, as JSON and as the exact string sent.
	Body        json.RawMessage `json:"body"`
	WireRequest string          `json:"wire_request"`
}

// IntentResultStatus is the "status" of an intent result the sampler itself writes. Nested
// effects store remote-reported results that may carry other statuses.
type IntentResultStatus string

const (
	IntentResultRejected  IntentResultStatus = "rejected"
	IntentResultAbandoned IntentResultStatus = "abandoned"
)

// NestedOutcome is the "outcome" of a nested executor effect's stored result. The rest of that
// result is the per-method remote RPC result or its failure evidence.
type NestedOutcome string

const (
	NestedCompleted     NestedOutcome = "completed"
	NestedRejected      NestedOutcome = "rejected"
	NestedUnknown       NestedOutcome = "unknown"
	NestedNotDispatched NestedOutcome = "not_dispatched"
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
	Status         protocol.ProcessStatus
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
	Status         protocol.CellStatus
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

type LostOwnershipError struct{ Reason string }

func (e *LostOwnershipError) Error() string {
	if e.Reason == "" {
		return "sampler generation is stale or expired"
	}
	return e.Reason
}

type InterruptPendingError struct{}

func (InterruptPendingError) Error() string {
	return "an interrupt is pending; the next dispatch is blocked"
}

func (state *ExecutionState) protocolState() *protocol.ExecutionState {
	if state == nil {
		return nil
	}
	result := &protocol.ExecutionState{}
	for _, cell := range state.Cells {
		result.Cells = append(result.Cells, protocol.ActiveCell{ID: cell.ID, Status: cell.Status})
	}
	for _, process := range state.Processes {
		result.Processes = append(result.Processes, protocol.ActiveProcess{ModelSessionID: process.ModelSessionID, Status: process.Status, TTY: process.TTY, ExitCode: process.ExitCode})
	}
	return result
}

var (
	ErrConflict  = errors.New("resource conflicts with an existing request")
	ErrForbidden = errors.New("resource belongs to another account")
)
