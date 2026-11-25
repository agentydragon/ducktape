package protocol

import "encoding/json"

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
	OriginAt(int) RecordOrigin
	Clone() Context
	AppendRecords([]json.RawMessage) error
	AppendContextRecords([]ContextRecord) error
	AppendUser([]ContentPart, RecordOrigin) error
	AppendNotice([]ContentPart, RecordOrigin) error
	// AddEnvironment places a rendered environment update in native context.
	// afterCompaction is true only for a newly validated replacement.
	AddEnvironment(parts []ContentPart, afterCompaction bool, origin RecordOrigin) error
	AppendToolResult(ToolInvocation, []ContentPart, RecordOrigin) error
	ChangeEffort(string) error
	PendingCalls() ([]ToolInvocation, error)
	FindCall(string) (ToolInvocation, error)
	CheckSettled() error
	TurnComplete() bool
}

type RecordOriginKind string

const (
	RecordOriginUnknown           RecordOriginKind = ""
	RecordOriginEnvironment       RecordOriginKind = "environment"
	RecordOriginExecutionSnapshot RecordOriginKind = "execution_snapshot"
	RecordOriginUserInput         RecordOriginKind = "user_input"
	RecordOriginProviderResult    RecordOriginKind = "provider_result"
	RecordOriginToolResult        RecordOriginKind = "tool_result"
	RecordOriginCompaction        RecordOriginKind = "compaction"
)

type RecordOriginProjection string

const (
	RecordProjectionTruncated       RecordOriginProjection = "truncated"
	RecordProjectionEnvironmentDiff RecordOriginProjection = "environment_diff"
)

// RecordOrigin identifies the sampler object or event represented by a record.
// SourceID references an existing command or intent, as determined by Kind.
// ObjectID instead references an immutable sampler content object.
type RecordOrigin struct {
	Kind       RecordOriginKind       `json:"kind"`
	SourceID   string                 `json:"source_id,omitempty"`
	ObjectID   string                 `json:"object_id,omitempty"`
	Projection RecordOriginProjection `json:"projection,omitempty"`
}

// ContextRecord couples a native record to its source. Provider request codecs
// only receive the native record.
type ContextRecord struct {
	Native json.RawMessage
	Origin RecordOrigin
}

func ContextRecords(records []json.RawMessage, origin RecordOrigin) []ContextRecord {
	out := make([]ContextRecord, len(records))
	for i, native := range records {
		out[i] = ContextRecord{Native: native, Origin: origin}
	}
	return out
}

type SessionAttribution struct {
	SessionID string
	ThreadID  string
	TurnID    string
}
