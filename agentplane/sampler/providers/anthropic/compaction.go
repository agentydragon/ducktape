package anthropic

import (
	"encoding/json"
	"errors"
	"slices"

	"github.com/agentydragon/ducktape/agentplane/sampler/protocol"
)

type CompactionPlan struct {
	Body Request

	original      *Context
	cut           int
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
	originalContext := params.Context.Clone().(*Context)
	prefix := &Context{messages: originalContext.messages[:cut:cut]}
	if err := prefix.CheckSettled(); err != nil {
		return nil, err
	}
	params.Context = prefix
	body, err := BuildRequest(params, config, previousMessageID)
	if err != nil {
		return nil, err
	}
	body.Compaction = &CompactionDirective{Type: "summarize"}
	body.MaxTokens = config.Compaction.MaxTokens
	var restore *string
	if config.PerMessageEffort {
		restore = &restoreEffort
	}
	return &CompactionPlan{Body: body, original: originalContext, cut: cut, restoreEffort: restore}, nil
}

func (p *CompactionPlan) Apply(records []json.RawMessage, audit *Audit, events []json.RawMessage, intentID string) (*Context, error) {
	if p == nil || p.original == nil {
		return nil, errors.New("Anthropic compaction plan is unavailable")
	}
	reject := func(reason string) (*Context, error) {
		var response any
		if audit != nil {
			response = struct {
				ContextTokens     *int              `json:"context_tokens"`
				Events            []json.RawMessage `json:"events"`
				HTTPStatus        int               `json:"http_status"`
				ProviderRequestID string            `json:"provider_request_id"`
				Replacement       *Context          `json:"replacement,omitempty"`
				Response          json.RawMessage   `json:"response"`
			}{audit.ContextTokens, events, audit.HTTPStatus, audit.ProviderRequestID, audit.Replacement, audit.Response}
		}
		return nil, &protocol.KnownFailureError{Evidence: map[string]any{"reason": reason, "response": response}}
	}
	if len(records) != 1 {
		return reject("compaction must produce exactly one signed compaction message")
	}
	if err := validateAnthropicMessage(records[0]); err != nil {
		return reject("compaction response is not a valid native message")
	}
	fields := messageFields(records[0])
	if roleOf(fields) != RoleAssistant || stopReasonOf(fields) != StopCompaction {
		return reject("compaction must produce exactly one signed compaction message")
	}
	blocks, err := anthropicContentBlocks(fields["content"])
	if err != nil || len(blocks) != 1 {
		return reject("compaction must produce exactly one signed compaction block")
	}
	var block map[string]json.RawMessage
	if err := json.Unmarshal(blocks[0], &block); err != nil || blockTypeOf(block) != BlockCompaction || rawString(block, "content") == "" || rawString(block, "signature") == "" {
		return reject("compaction must produce exactly one signed compaction block")
	}
	handoff := protocol.ContextRecord{
		Native: slices.Clone(records[0]),
		Origin: protocol.RecordOrigin{Kind: protocol.RecordOriginCompaction, SourceID: intentID},
	}
	replacement := &Context{messages: []protocol.ContextRecord{handoff}, PendingEffort: cloneString(p.original.PendingEffort)}
	replacement.messages = append(replacement.messages, p.original.messages[p.cut:]...)
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
	replacementJSON, _ := json.Marshal(replacementMessages)
	originalJSON, _ := json.Marshal(originalMessages)
	if len(replacementJSON) >= len(originalJSON) {
		return reject("compaction did not reduce serialized model context")
	}
	if audit != nil {
		audit.Replacement = replacement
	}
	return replacement, nil
}

func cloneString(value *string) *string {
	if value == nil {
		return nil
	}
	copy := *value
	return &copy
}
