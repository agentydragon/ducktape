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
	Head       string
	Count      int
	OriginHead string
	Origins    protocol.RecordOrigins
}

type nativeControl struct {
	PendingEffort     *string                 `json:"pending_effort,omitempty"`
	ExecutionSnapshot *modelExecutionSnapshot `json:"execution_snapshot,omitempty"`
	OriginContextID   string                  `json:"origin_context_id,omitempty"`
}

const originContentKind content.ContentKind = "sampler_record_origin"

// Bind metadata to a native occurrence by both its index and content identity.
type storedOrigin struct {
	protocol.OriginEntry
	NativeID content.ContentID `json:"native_id"`
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

func decodeNativeRecords(api protocol.APIStyle, records []json.RawMessage) (protocol.Context, error) {
	state, err := newNativeContext(api)
	if err != nil {
		return nil, err
	}
	if err := state.AppendRecords(records); err != nil {
		return nil, err
	}
	return state, nil
}

func (conversation *Conversation) storeContext(store *contentdb.Store, native protocol.Context) (string, error) {
	if conversation.storedContexts == nil {
		conversation.storedContexts = map[protocol.Context]contextCheckpoint{}
	}
	base := conversation.storedContexts[native]
	records, err := native.RecordsFrom(base.Count)
	if err != nil {
		return "", err
	}
	ids := make([]content.ContentID, 0, len(records))
	for _, record := range records {
		id, err := providerrequest.StoreRecord(store, native.API(), record)
		if err != nil {
			return "", err
		}
		ids = append(ids, id)
	}
	entries := native.Origins().EntriesFrom(base.Count)
	if base.Origins.Len()+len(entries) != native.Origins().Len() || !native.Origins().Extends(&base.Origins) {
		return "", fmt.Errorf("stored origin prefix changed: %w", protocol.ErrPrecondition)
	}
	originIDs := make([]content.ContentID, 0, len(entries))
	for _, entry := range entries {
		index := entry.Index - base.Count
		if index < 0 || index >= len(ids) || entry.Origin.Kind == protocol.RecordOriginUnknown {
			return "", fmt.Errorf("invalid origin for record %d: %w", entry.Index, protocol.ErrInvalid)
		}
		payload, err := json.Marshal(storedOrigin{OriginEntry: entry, NativeID: ids[index]})
		if err != nil {
			return "", err
		}
		id, err := store.PutContent(originContentKind, payload)
		if err != nil {
			return "", err
		}
		originIDs = append(originIDs, id)
	}
	head, err := store.AppendContextIDs(native.API(), base.Head, base.Count, ids)
	if err != nil {
		return "", err
	}
	originHead := base.OriginHead
	if len(originIDs) > 0 {
		originHead, err = store.AppendContextIDs(native.API(), originHead, base.Origins.Len(), originIDs)
		if err != nil {
			return "", err
		}
	}
	conversation.storedContexts[native] = contextCheckpoint{
		Head: head, Count: native.RecordCount(), OriginHead: originHead, Origins: native.Origins().Clone(),
	}
	return head, nil
}

func (conversation *Conversation) contextControl(native protocol.Context) (json.RawMessage, error) {
	control := nativeControl{
		ExecutionSnapshot: conversation.executionSnapshots[native],
		OriginContextID:   conversation.storedContexts[native].OriginHead,
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
	ids, err := store.LoadContext(id)
	if err != nil {
		return nil, err
	}
	originIDs, err := store.LoadContext(state.OriginContextID)
	if err != nil {
		return nil, err
	}
	if err := store.Prefetch(append(ids, originIDs...)); err != nil {
		return nil, err
	}
	records, err := providerrequest.RestoreRecords(store, api, ids)
	if err != nil {
		return nil, err
	}
	native, err := decodeNativeRecords(api, records)
	if err != nil {
		return nil, err
	}
	entries := make([]protocol.OriginEntry, 0, len(originIDs))
	for _, originID := range originIDs {
		row, err := store.GetContent(originID)
		if err != nil {
			return nil, err
		}
		var origin storedOrigin
		if row.Kind != originContentKind || json.Unmarshal(row.Payload, &origin) != nil ||
			origin.Index < 0 || origin.Index >= len(ids) || origin.NativeID != ids[origin.Index] {
			return nil, fmt.Errorf("origin %s does not match its native record: %w", originID, protocol.ErrInvalid)
		}
		entries = append(entries, origin.OriginEntry)
	}
	if err := native.Origins().Restore(entries, native.RecordCount()); err != nil {
		return nil, err
	}
	if c, ok := native.(*anthropic.Context); ok {
		c.PendingEffort = state.PendingEffort
	}
	conversation.executionSnapshots[native] = state.ExecutionSnapshot
	conversation.storedContexts[native] = contextCheckpoint{
		Head: id, Count: native.RecordCount(), OriginHead: state.OriginContextID, Origins: native.Origins().Clone(),
	}
	return native, nil
}
