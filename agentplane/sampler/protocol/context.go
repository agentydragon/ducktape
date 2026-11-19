package protocol

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/executionstate"
)

type APIStyle string

const (
	APIStyleResponses         APIStyle = "responses"
	APIStyleChatCompletions   APIStyle = "chat_completions"
	APIStyleAnthropicMessages APIStyle = "anthropic_messages"
)

type ExecEncoding string

const (
	ExecEncodingFreeform ExecEncoding = "freeform"
	ExecEncodingJSON     ExecEncoding = "json"
)

type Context interface {
	json.Marshaler
	API() APIStyle
	Records() ([]json.RawMessage, error)
	RecordsFrom(int) ([]json.RawMessage, error)
	RecordCount() int
	Clone() Context
	AppendRecords([]json.RawMessage) error
	AppendUser([]ContentPart) error
	AppendNotice([]ContentPart) error
	AppendToolResult(ToolInvocation, []ContentPart) error
	ChangeEffort(string) error
	PendingCalls() ([]ToolInvocation, error)
	FindCall(string) (ToolInvocation, error)
	CheckSettled() error
	TurnComplete() bool
}

// ExecutionState contains only the handles visible to a model.
type ExecutionState struct {
	Cells     []ActiveCell
	Processes []ActiveProcess
}
type (
	ActiveCell struct {
		ID     string
		Status executionstate.CellStatus
	}
	ActiveProcess struct {
		ModelSessionID *int
		Status         executionstate.ProcessStatus
		TTY            bool
		ExitCode       *int
	}
)

type SessionAttribution struct {
	SessionID string
	ThreadID  string
	TurnID    string
}
