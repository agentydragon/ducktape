package protocol

import (
	"cmp"
	"encoding/json"
	"fmt"
	"slices"

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
	Origins() *RecordOrigins
	Clone() Context
	AppendRecords([]json.RawMessage) error
	AppendUser([]ContentPart) error
	AppendNotice([]ContentPart) error
	// AddEnvironment inserts current Session context and returns its native record index.
	// afterCompaction is true only for a newly validated replacement.
	AddEnvironment(parts []ContentPart, afterCompaction bool) (int, error)
	AppendToolResult(ToolInvocation, []ContentPart) error
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
)

type RecordOriginProjection string

// RecordProjectionTruncated identifies a shortened copy of the source content.
const RecordProjectionTruncated RecordOriginProjection = "truncated"

// RecordOrigin identifies the sampler object or event represented by a record.
// The origin kind determines the namespace of SourceID. Projection describes
// transformations beyond the normal provider encoding.
type RecordOrigin struct {
	Kind       RecordOriginKind       `json:"kind"`
	SourceID   string                 `json:"source_id,omitempty"`
	Projection RecordOriginProjection `json:"projection,omitempty"`
}

type OriginEntry struct {
	Index  int          `json:"index"`
	Origin RecordOrigin `json:"origin"`
}

// RecordOrigins tracks semantic origins at zero-based native record indices.
// The store persists them separately from provider-native records.
type RecordOrigins struct {
	entries []OriginEntry
}

func (o *RecordOrigins) Origins() *RecordOrigins { return o }

func (o *RecordOrigins) OriginAt(index int) RecordOrigin {
	at, found := o.find(index)
	if !found {
		return RecordOrigin{}
	}
	return o.entries[at].Origin
}

func (o *RecordOrigins) EntriesFrom(recordIndex int) []OriginEntry {
	at, _ := o.find(recordIndex)
	return slices.Clone(o.entries[at:])
}

func (o *RecordOrigins) Len() int { return len(o.entries) }

func (o *RecordOrigins) Restore(entries []OriginEntry, count int) error {
	previous := -1
	for _, entry := range entries {
		if entry.Index <= previous || entry.Index >= count || entry.Origin.Kind == RecordOriginUnknown {
			return fmt.Errorf("invalid origin for record index %d: %w", entry.Index, ErrInvalid)
		}
		previous = entry.Index
	}
	o.entries = slices.Clone(entries)
	return nil
}

func (o *RecordOrigins) Equal(other *RecordOrigins) bool {
	return len(o.entries) == len(other.entries) && o.Extends(other)
}

func (o *RecordOrigins) Clone() RecordOrigins {
	return RecordOrigins{entries: slices.Clip(o.entries)}
}

// Extends reports whether the committed origin prefix is still present.
func (o *RecordOrigins) Extends(prefix *RecordOrigins) bool {
	length := len(prefix.entries)
	return len(o.entries) >= length && (length == 0 || &o.entries[0] == &prefix.entries[0] ||
		slices.Equal(o.entries[:length], prefix.entries))
}

// Insert shifts existing indices when the caller inserts a new native record.
func (o *RecordOrigins) Insert(index int, origin RecordOrigin) {
	at, _ := o.find(index)
	entry := OriginEntry{Index: index, Origin: origin}
	if at == len(o.entries) {
		o.entries = append(o.entries, entry)
		return
	}
	entries := make([]OriginEntry, len(o.entries)+1)
	copy(entries, o.entries[:at])
	entries[at] = entry
	copy(entries[at+1:], o.entries[at:])
	for i := at + 1; i < len(entries); i++ {
		entries[i].Index++
	}
	o.entries = entries
}

// RetainSuffix maps the remaining records into a replacement with a new prefix.
func (o *RecordOrigins) RetainSuffix(start, prefix int) RecordOrigins {
	remaining := o.EntriesFrom(start)
	for i := range remaining {
		remaining[i].Index += prefix - start
	}
	return RecordOrigins{entries: remaining}
}

func (o *RecordOrigins) find(index int) (int, bool) {
	return slices.BinarySearchFunc(o.entries, index, func(entry OriginEntry, index int) int {
		return cmp.Compare(entry.Index, index)
	})
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
