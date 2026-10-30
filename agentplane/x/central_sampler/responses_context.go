package centralsampler

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
)

// ResponsesContext retains only native Responses records. Request-only items and the markers used
// by the Anthropic adapter belong to their adapter and are never replayed from this context.
type ResponsesContext struct {
	Items []ResponsesItem `json:"items"`
}

func (*ResponsesContext) nativeContext() {}

func (*ResponsesContext) API() APIStyle { return APIStyleResponses }

func (state *ResponsesContext) UnmarshalJSON(data []byte) error {
	if len(bytes.TrimSpace(data)) == 0 || !bytes.HasPrefix(bytes.TrimSpace(data), []byte("{")) {
		return errors.New("Responses context must be an object")
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for name := range fields {
		if name != "items" {
			return fmt.Errorf("unknown Responses context field %q", name)
		}
	}
	rawItems, present := fields["items"]
	if !present {
		return errors.New("Responses context has no items field")
	}
	var records []json.RawMessage
	if err := json.Unmarshal(rawItems, &records); err != nil {
		return fmt.Errorf("Responses context items: %w", err)
	}
	var decoded ResponsesContext
	if err := decoded.AppendRecords(records); err != nil {
		return err
	}
	*state = decoded
	return nil
}

func (state ResponsesContext) MarshalJSON() ([]byte, error) {
	if _, err := state.Records(); err != nil {
		return nil, err
	}
	type wire struct {
		Items []ResponsesItem `json:"items"`
	}
	return json.Marshal(wire{Items: state.Items})
}

func (state *ResponsesContext) Records() ([]json.RawMessage, error) {
	if err := validateResponsesItems(state.Items); err != nil {
		return nil, err
	}
	records := make([]json.RawMessage, 0, len(state.Items))
	for _, item := range state.Items {
		encoded, err := json.Marshal(item)
		if err != nil {
			return nil, err
		}
		records = append(records, cloneJSON(encoded))
	}
	return records, nil
}

func (state *ResponsesContext) AppendRecords(records []json.RawMessage) error {
	appended := make([]ResponsesItem, 0, len(records))
	for index, record := range records {
		if len(bytes.TrimSpace(record)) == 0 || !bytes.HasPrefix(bytes.TrimSpace(record), []byte("{")) {
			return fmt.Errorf("Responses record %d must be an object", index+1)
		}
		var item ResponsesItem
		if err := json.Unmarshal(record, &item); err != nil {
			return fmt.Errorf("decode Responses record %d: %w", index+1, err)
		}
		if err := validateResponsesItem(item); err != nil {
			return fmt.Errorf("Responses record %d: %w", index+1, err)
		}
		appended = append(appended, item)
	}
	candidate := make([]ResponsesItem, 0, len(state.Items)+len(appended))
	candidate = append(candidate, state.Items...)
	candidate = append(candidate, appended...)
	if err := validateResponsesItems(candidate); err != nil {
		return err
	}
	state.Items = candidate
	return nil
}

func (state *ResponsesContext) AppendUser(parts []ContentPart) error {
	return state.appendMessage("user", parts)
}

func (state *ResponsesContext) AppendNotice(parts []ContentPart) error {
	return state.appendMessage("developer", parts)
}

func (state *ResponsesContext) appendMessage(role string, parts []ContentPart) error {
	if len(parts) == 1 && parts[0].Type == "input_text" && len(parts[0].Raw) == 0 {
		item, err := newResponsesMessage(role, parts[0].Text)
		if err != nil {
			return err
		}
		encoded, err := json.Marshal(item)
		if err != nil {
			return err
		}
		return state.AppendRecords([]json.RawMessage{encoded})
	}
	content, err := json.Marshal(append([]ContentPart{}, parts...))
	if err != nil {
		return err
	}
	item := ResponsesItem{Type: ResponsesItemMessage, Role: role, Content: content}
	encoded, err := json.Marshal(item)
	if err != nil {
		return err
	}
	return state.AppendRecords([]json.RawMessage{encoded})
}

func (*ResponsesContext) ChangeEffort(string) error { return nil }

func (*ResponsesContext) AppendExecutionState(*ExecutionState) error { return nil }

func (state *ResponsesContext) AppendToolResult(call ToolInvocation, parts []ContentPart) error {
	if !call.HasValidShape() {
		return fmt.Errorf("invalid tool invocation: %w", ErrInvalid)
	}
	known, err := state.FindCall(call.CallID)
	if err != nil {
		return err
	}
	if known.Encoding != call.Encoding || known.Name != call.Name || known.Input != call.Input ||
		(call.SourceRecord != 0 && known.SourceRecord != call.SourceRecord) {
		return fmt.Errorf("tool invocation provenance changed for %q: %w", call.CallID, ErrInvalid)
	}
	outputType := ResponsesItemFunctionOutput
	if call.Encoding == ExecEncodingFreeform {
		outputType = ResponsesItemCustomToolOutput
	}
	item := ResponsesItem{Type: outputType, CallID: call.CallID, Output: append([]ContentPart{}, parts...)}
	encoded, err := json.Marshal(item)
	if err != nil {
		return err
	}
	return state.AppendRecords([]json.RawMessage{encoded})
}

// FindCall returns a call by ID even after its first output. Responses allows asynchronous
// notifications to append further output records for the same call.
func (state *ResponsesContext) FindCall(callID string) (ToolInvocation, error) {
	if err := validateResponsesItems(state.Items); err != nil {
		return ToolInvocation{}, err
	}
	for index, item := range state.Items {
		call, ok := item.ToolCall()
		if !ok || call.CallID != callID {
			continue
		}
		call.SourceRecord = index + 1
		return call, nil
	}
	return ToolInvocation{}, ErrMissing
}

func (state *ResponsesContext) PendingCalls() ([]ToolInvocation, error) {
	for index, item := range state.Items {
		if err := validateResponsesItem(item); err != nil {
			return nil, fmt.Errorf("Responses record %d: %w", index+1, err)
		}
	}
	return responsesPendingCalls(state.Items)
}

func (state *ResponsesContext) CheckSettled() error {
	pending, err := state.PendingCalls()
	if err != nil {
		return err
	}
	if len(pending) == 0 {
		return nil
	}
	ids := make([]string, len(pending))
	for index, call := range pending {
		ids[index] = call.CallID
	}
	sort.Strings(ids)
	return &UnknownEffectError{Reason: fmt.Sprintf("unresolved tool calls %v", ids)}
}

func (state *ResponsesContext) TurnComplete() bool {
	if len(state.Items) == 0 || state.CheckSettled() != nil {
		return false
	}
	last := state.Items[len(state.Items)-1]
	return last.Type == ResponsesItemMessage && last.Role == "assistant"
}

func validateResponsesItems(items []ResponsesItem) error {
	for index, item := range items {
		if err := validateResponsesItem(item); err != nil {
			return fmt.Errorf("Responses record %d: %w", index+1, err)
		}
	}
	_, err := responsesPendingCalls(items)
	return err
}

func validateResponsesItem(item ResponsesItem) error {
	switch item.Type {
	case ResponsesItemMessage, ResponsesItemReasoning, ResponsesItemCustomToolCall,
		ResponsesItemCustomToolOutput, ResponsesItemFunctionCall, ResponsesItemFunctionOutput,
		ResponsesItemCompaction:
	default:
		return fmt.Errorf("unsupported native Responses item type %q: %w", item.Type, ErrInvalid)
	}
	if _, isCall := item.ToolCall(); isCall && !item.HasValidToolCallShape() {
		return fmt.Errorf("malformed tool call: %w", ErrInvalid)
	}
	return nil
}

// responsesPendingCalls validates call/output links and returns unanswered calls in their native
// record order. Repeated outputs are retained and accepted for asynchronous tool notifications.
func responsesPendingCalls(items []ResponsesItem) ([]ToolInvocation, error) {
	seen := make(map[string]ToolInvocation)
	completed := make(map[string]struct{})
	var calls []ToolInvocation
	for index, item := range items {
		if call, ok := item.ToolCall(); ok {
			call.SourceRecord = index + 1
			if _, exists := seen[call.CallID]; exists {
				return nil, &UnknownEffectError{Reason: "duplicate tool call " + call.CallID}
			}
			seen[call.CallID] = call
			calls = append(calls, call)
			continue
		}
		if !item.IsToolOutput() {
			continue
		}
		call, exists := seen[item.CallID]
		if !exists {
			return nil, &UnknownEffectError{Reason: "orphan tool output " + item.CallID}
		}
		want := ResponsesItemFunctionOutput
		if call.Encoding == ExecEncodingFreeform {
			want = ResponsesItemCustomToolOutput
		}
		if item.Type != want {
			return nil, &UnknownEffectError{Reason: fmt.Sprintf("tool output %s does not match %s call %s", item.Type, call.Encoding, item.CallID)}
		}
		completed[item.CallID] = struct{}{}
	}
	pending := make([]ToolInvocation, 0, len(calls))
	for _, call := range calls {
		if _, done := completed[call.CallID]; !done {
			pending = append(pending, call)
		}
	}
	return pending, nil
}
