package sessionstore

import (
	"encoding/json"

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
	head, err := store.AppendContextIDs(native.API(), base.Head, base.Count, ids)
	if err != nil {
		return "", err
	}
	conversation.storedContexts[native] = contextCheckpoint{Head: head, Count: native.RecordCount()}
	return head, nil
}

func (conversation *Conversation) contextControl(native protocol.Context) (json.RawMessage, error) {
	control := nativeControl{ExecutionSnapshot: conversation.executionSnapshots[native]}
	if c, ok := native.(*anthropic.Context); ok {
		control.PendingEffort = c.PendingEffort
	}
	return json.Marshal(control)
}

func (conversation *Conversation) loadNativeContext(store *contentdb.Store, api protocol.APIStyle, id string, control json.RawMessage) (protocol.Context, error) {
	ids, err := store.LoadContext(id)
	if err != nil {
		return nil, err
	}
	if err := store.Prefetch(ids); err != nil {
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
	if c, ok := native.(*anthropic.Context); ok && len(control) > 0 {
		var state nativeControl
		if err := json.Unmarshal(control, &state); err != nil {
			return nil, err
		}
		c.PendingEffort = state.PendingEffort
	}
	return native, nil
}

func (conversation *Conversation) restoreExecutionSnapshot(native protocol.Context, raw json.RawMessage) error {
	if len(raw) == 0 {
		return nil
	}
	var control nativeControl
	if err := json.Unmarshal(raw, &control); err != nil {
		return err
	}
	conversation.executionSnapshots[native] = control.ExecutionSnapshot
	return nil
}
