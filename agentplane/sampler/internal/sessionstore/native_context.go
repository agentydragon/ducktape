package sessionstore

import (
	"encoding/json"
	"fmt"

	"github.com/agentydragon/ducktape/agentplane/sampler/internal/content"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/providerrequest"
	"github.com/agentydragon/ducktape/agentplane/sampler/internal/sessionstore/internal/contentdb"
	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/anthropic"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/chatcompletions"
	"github.com/agentydragon/ducktape/agentplane/sampler/providers/responses"
)

type contextCheckpoint struct {
	Head  string
	Count int
}

type nativeControl struct {
	PendingEffort     *string                 `json:"pending_effort,omitempty"`
	ExecutionSnapshot *modelExecutionSnapshot `json:"execution_snapshot,omitempty"`
}

const contextRecordKind content.ContentKind = "sampler_context_record"

// A context node stores this reference pair when the native record has a source.
// Provider requests continue to point at the native content directly.
type storedContextRecord struct {
	NativeID content.ContentID     `json:"native_id"`
	Origin   protocol.RecordOrigin `json:"origin"`
}

func (r storedContextRecord) validate() error {
	if r.NativeID == "" || r.Origin.Kind == protocol.RecordOriginUnknown ||
		(r.Origin.SourceID == "") == (r.Origin.ObjectID == "") {
		return fmt.Errorf("invalid sourced context record: %w", protocol.ErrInvalid)
	}
	return nil
}

func newNativeContext(api protocol.APIStyle) (protocol.Context, error) {
	switch api {
	case protocol.APIStyleResponses:
		return &responses.Context{}, nil
	case protocol.APIStyleAnthropicMessages:
		return &anthropic.Context{}, nil
	case protocol.APIStyleChatCompletions:
		return &chatcompletions.Context{}, nil
	default:
		return nil, protocol.ErrInvalid
	}
}

func (conversation *Conversation) storeContext(store *contentdb.Store, native protocol.Context, writeSources bool) (string, error) {
	if conversation.storedContexts == nil {
		conversation.storedContexts = map[protocol.Context]contextCheckpoint{}
	}
	base := conversation.storedContexts[native]
	records, err := native.RecordsFrom(base.Count)
	if err != nil {
		return "", err
	}
	ids := make([]content.ContentID, 0, len(records))
	for i, record := range records {
		id, err := providerrequest.StoreRecord(store, native.API(), record)
		if err != nil {
			return "", err
		}
		origin := native.OriginAt(base.Count + i)
		if writeSources && origin.Kind != protocol.RecordOriginUnknown {
			entry := storedContextRecord{NativeID: id, Origin: origin}
			if err := entry.validate(); err != nil {
				return "", err
			}
			id, err = content.PutJSON(store, contextRecordKind, entry)
			if err != nil {
				return "", err
			}
		}
		ids = append(ids, id)
	}
	head, err := store.AppendContextIDs(native.API(), base.Head, base.Count, ids)
	if err != nil {
		return "", err
	}
	conversation.storedContexts[native] = contextCheckpoint{Head: head, Count: native.RecordCount()}
	return head, nil
}

func (conversation *Conversation) contextControl(native protocol.Context) (json.RawMessage, error) {
	control := nativeControl{
		ExecutionSnapshot: conversation.executionSnapshots[native],
	}
	if c, ok := native.(*anthropic.Context); ok {
		control.PendingEffort = c.PendingEffort
	}
	return json.Marshal(control)
}

func (conversation *Conversation) loadNativeContext(store *contentdb.Store, api protocol.APIStyle, id string, control json.RawMessage) (protocol.Context, error) {
	var state nativeControl
	if len(control) > 0 {
		if err := json.Unmarshal(control, &state); err != nil {
			return nil, err
		}
	}
	records, err := loadContextRecords(store, api, id)
	if err != nil {
		return nil, err
	}
	native, err := newNativeContext(api)
	if err != nil {
		return nil, err
	}
	if err := native.AppendContextRecords(records); err != nil {
		return nil, err
	}
	if c, ok := native.(*anthropic.Context); ok {
		c.PendingEffort = state.PendingEffort
	}
	conversation.executionSnapshots[native] = state.ExecutionSnapshot
	conversation.storedContexts[native] = contextCheckpoint{Head: id, Count: native.RecordCount()}
	return native, nil
}

// nativeContextReferences unwraps source-bearing entries. Bare native references
// are also valid and represent records with unknown origin.
func nativeContextReferences(store *contentdb.Store, head string) ([]storedContextRecord, error) {
	ids, err := store.LoadContext(head)
	if err != nil {
		return nil, err
	}
	if err := store.Prefetch(ids); err != nil {
		return nil, err
	}
	refs := make([]storedContextRecord, len(ids))
	for i, id := range ids {
		row, err := store.GetContent(id)
		if err != nil {
			return nil, err
		}
		if row.Kind != contextRecordKind {
			refs[i].NativeID = id
			continue
		}
		if err := json.Unmarshal(row.Payload, &refs[i]); err != nil {
			return nil, err
		}
		if err := refs[i].validate(); err != nil {
			return nil, err
		}
	}
	return refs, nil
}

func loadContextRecords(store *contentdb.Store, api protocol.APIStyle, head string) ([]protocol.ContextRecord, error) {
	refs, err := nativeContextReferences(store, head)
	if err != nil {
		return nil, err
	}
	ids := make([]content.ContentID, len(refs))
	for i, ref := range refs {
		ids[i] = ref.NativeID
	}
	native, err := providerrequest.RestoreRecords(store, api, ids)
	if err != nil {
		return nil, err
	}
	records := make([]protocol.ContextRecord, len(refs))
	for i, ref := range refs {
		records[i] = protocol.ContextRecord{Native: native[i], Origin: ref.Origin}
	}
	return records, nil
}

func sameNativeContextRefs(store *contentdb.Store, left, right string) (bool, error) {
	first, err := nativeContextReferences(store, left)
	if err != nil {
		return false, err
	}
	second, err := nativeContextReferences(store, right)
	if err != nil {
		return false, err
	}
	if len(first) != len(second) {
		return false, nil
	}
	for i, ref := range first {
		if ref.NativeID != second[i].NativeID {
			return false, nil
		}
	}
	return true, nil
}
