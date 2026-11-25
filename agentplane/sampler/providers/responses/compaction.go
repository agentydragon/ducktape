package responses

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

// ValidateCompaction validates provider records before the caller commits them as the active
// context. The append-only transcript remains owned by the caller.
func ValidateCompaction(original *Context, records []json.RawMessage, audit *Audit, events []Event, intentID string) (*Context, error) {
	if original == nil {
		return nil, protocol.ErrInvalid
	}
	knownFailure := func(reason string) error {
		var response any
		if audit != nil {
			response = map[string]any{"events": events, "response": audit.Response}
		}
		return &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason, "response": response}}
	}
	if len(records) != 1 {
		return nil, knownFailure("compaction must produce exactly one compaction item and no tools")
	}
	var item Item
	if err := json.Unmarshal(records[0], &item); err != nil {
		return nil, err
	}
	if item.Type != ItemCompaction {
		return nil, knownFailure("compaction must produce exactly one compaction item and no tools")
	}
	if item.EncryptedContent == "" {
		return nil, knownFailure("compaction must include opaque encrypted_content")
	}
	item.origin = protocol.RecordOrigin{Kind: protocol.RecordOriginCompaction, SourceID: intentID}
	compacted := []Item{item}
	if contextBytes(compacted) >= contextBytes(original.items) {
		return nil, knownFailure("compaction did not reduce serialized model context")
	}
	replacement := &Context{items: compacted}
	if _, err := replacement.Records(); err != nil {
		return nil, err
	}
	return replacement, nil
}

func contextBytes(items []Item) int {
	data, _ := json.Marshal(items)
	return len(data)
}
