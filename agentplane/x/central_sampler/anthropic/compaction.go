package anthropic

import (
	"encoding/json"
	"errors"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

type CompactionPlan struct {
	Body map[string]any

	original      *Context
	suffix        []json.RawMessage
	restoreEffort *string
}

func PrepareCompaction(params RequestParams, config Config, previousMessageID, restoreEffort string) (*CompactionPlan, error) {
	if err := config.Validate(); err != nil {
		return nil, err
	}
	if config.Compaction == nil {
		return nil, &protocol.KnownFailureError{Evidence: map[string]any{"reason": "Anthropic compaction is not enabled for this route"}}
	}
	if params.Context == nil {
		return nil, errors.New("Anthropic compaction requires a context")
	}
	records, err := params.Context.Records()
	if err != nil {
		return nil, err
	}
	cut := suffixStart(records, config.Compaction.KeepRecentTurns)
	// A kept system control would merge with the summarized prefix, breaking preserved thinking.
	for cut > 0 && cut < len(records) && isEffortControl(records[cut]) {
		cut = suffixStart(records[:cut], 1)
	}
	if cut == 0 {
		return nil, &protocol.KnownFailureError{Evidence: map[string]any{"reason": "no settled prefix remains after keeping recent turns"}}
	}
	originalContext := &Context{Messages: cloneRecords(records), PendingEffort: cloneString(params.Context.PendingEffort)}
	prefix := &Context{Messages: records[:cut]}
	if err := prefix.CheckSettled(); err != nil {
		return nil, err
	}
	params.Context = prefix
	body, err := BuildRequest(params, config, previousMessageID)
	if err != nil {
		return nil, err
	}
	body["compaction"] = map[string]any{"type": "summarize"}
	body["max_tokens"] = config.Compaction.MaxTokens
	var restore *string
	if config.PerMessageEffort {
		restore = &restoreEffort
	}
	return &CompactionPlan{Body: body, original: originalContext, suffix: cloneRecords(records[cut:]), restoreEffort: restore}, nil
}

func (p *CompactionPlan) Apply(records []json.RawMessage, audit map[string]any) (*Context, error) {
	if p == nil || p.original == nil {
		return nil, errors.New("Anthropic compaction plan is unavailable")
	}
	reject := func(reason string) (*Context, error) {
		return nil, &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason, "response": audit}}
	}
	if len(records) != 1 {
		return reject("compaction must produce exactly one signed compaction message")
	}
	if err := validateAnthropicMessage(records[0]); err != nil {
		return reject("compaction response is not a valid native message")
	}
	fields := messageFields(records[0])
	if rawString(fields, "role") != "assistant" || rawString(fields, "stop_reason") != "compaction" {
		return reject("compaction must produce exactly one signed compaction message")
	}
	blocks, err := anthropicContentBlocks(fields["content"])
	if err != nil || len(blocks) != 1 {
		return reject("compaction must produce exactly one signed compaction block")
	}
	var block map[string]json.RawMessage
	if err := json.Unmarshal(blocks[0], &block); err != nil || rawString(block, "type") != "compaction" || rawString(block, "content") == "" || rawString(block, "signature") == "" {
		return reject("compaction must produce exactly one signed compaction block")
	}
	replacement := &Context{Messages: []json.RawMessage{protocol.CloneJSON(records[0])}, PendingEffort: cloneString(p.original.PendingEffort)}
	replacement.Messages = append(replacement.Messages, cloneRecords(p.suffix)...)
	if p.restoreEffort != nil {
		replacement.PendingEffort = cloneString(p.restoreEffort)
	}
	originalMessages, err := p.original.WireMessages()
	if err != nil {
		return nil, err
	}
	replacementMessages, err := replacement.WireMessages()
	if err != nil {
		return nil, err
	}
	if len(protocol.MustJSON(replacementMessages)) >= len(protocol.MustJSON(originalMessages)) {
		return reject("compaction did not reduce serialized model context")
	}
	if audit != nil {
		audit["replacement"] = replacement
	}
	return replacement, nil
}

func cloneRecords(records []json.RawMessage) []json.RawMessage {
	cloned := make([]json.RawMessage, len(records))
	for i, record := range records {
		cloned[i] = protocol.CloneJSON(record)
	}
	return cloned
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}
