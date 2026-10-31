package responses

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

// ValidateCompaction validates provider records before the caller commits them as the active
// context. The append-only transcript remains owned by the caller.
func ValidateCompaction(original *Context, records []json.RawMessage, audit *Audit, environmentText string) (*Context, error) {
	if original == nil {
		return nil, protocol.ErrInvalid
	}
	knownFailure := func(reason string) error {
		return &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason, "response": audit}}
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
	compacted := []Item{item}
	if contextBytes(compacted) >= contextBytes(original.Items) {
		return nil, knownFailure("compaction did not reduce serialized model context")
	}
	environment, err := Message("user", environmentText)
	if err != nil {
		return nil, err
	}
	replacement := &Context{Items: append([]Item{environment}, compacted...)}
	if _, err := replacement.Records(); err != nil {
		return nil, err
	}
	return replacement, nil
}

func contextBytes(items []Item) int {
	data, _ := json.Marshal(items)
	return len(data)
}
