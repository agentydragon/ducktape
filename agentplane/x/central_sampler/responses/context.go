package responses

import (
	"bytes"
	"encoding/json"
	"fmt"
	"sort"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
)

// Context retains only native Responses records. Request-only items and adapter markers are not
// replayed from this context.
type Context struct {
	Items []Item `json:"items"`
}

func (*Context) API() protocol.APIStyle { return protocol.APIStyleResponses }

func (state *Context) UnmarshalJSON(data []byte) error {
	if len(bytes.TrimSpace(data)) == 0 || !bytes.HasPrefix(bytes.TrimSpace(data), []byte("{")) {
		return fmt.Errorf("Responses context must be an object: %w", protocol.ErrInvalid)
	}
	var fields map[string]json.RawMessage
	if err := json.Unmarshal(data, &fields); err != nil {
		return err
	}
	for name := range fields {
		if name != "items" {
			return fmt.Errorf("unknown Responses context field %q: %w", name, protocol.ErrInvalid)
		}
	}
	rawItems, present := fields["items"]
	if !present {
		return fmt.Errorf("Responses context has no items field: %w", protocol.ErrInvalid)
	}
	var records []json.RawMessage
	if err := json.Unmarshal(rawItems, &records); err != nil {
		return fmt.Errorf("Responses context items: %w", err)
	}
	var decoded Context
	if err := decoded.AppendRecords(records); err != nil {
		return err
	}
	*state = decoded
	return nil
}

func (state Context) MarshalJSON() ([]byte, error) {
	if _, err := state.Records(); err != nil {
		return nil, err
	}
	type wire struct {
		Items []Item `json:"items"`
	}
	return json.Marshal(wire{Items: state.Items})
}

func (state *Context) Records() ([]json.RawMessage, error) {
	if err := validateItems(state.Items); err != nil {
		return nil, err
	}
	records := make([]json.RawMessage, 0, len(state.Items))
	for _, item := range state.Items {
		encoded, err := json.Marshal(item)
		if err != nil {
			return nil, err
		}
		records = append(records, protocol.CloneJSON(encoded))
	}
	return records, nil
}

func (state *Context) AppendRecords(records []json.RawMessage) error {
	appended := make([]Item, 0, len(records))
	for index, record := range records {
		if len(bytes.TrimSpace(record)) == 0 || !bytes.HasPrefix(bytes.TrimSpace(record), []byte("{")) {
			return fmt.Errorf("Responses record %d must be an object: %w", index+1, protocol.ErrInvalid)
		}
		var item Item
		if err := json.Unmarshal(record, &item); err != nil {
			return fmt.Errorf("decode Responses record %d: %w", index+1, err)
		}
		if err := validateItem(item); err != nil {
			return fmt.Errorf("Responses record %d: %w", index+1, err)
		}
		appended = append(appended, item)
	}
	candidate := make([]Item, 0, len(state.Items)+len(appended))
	candidate = append(candidate, state.Items...)
	candidate = append(candidate, appended...)
	if err := validateItems(candidate); err != nil {
		return err
	}
	state.Items = candidate
	return nil
}

func (state *Context) AppendUser(parts []protocol.ContentPart) error {
	return state.appendMessage("user", parts)
}

func (state *Context) AppendNotice(parts []protocol.ContentPart) error {
	return state.appendMessage("developer", parts)
}

func (state *Context) appendMessage(role string, parts []protocol.ContentPart) error {
	if len(parts) == 1 && parts[0].Type == "input_text" && len(parts[0].Raw) == 0 {
		item, err := Message(role, parts[0].Text)
		if err != nil {
			return err
		}
		encoded, err := json.Marshal(item)
		if err != nil {
			return err
		}
		return state.AppendRecords([]json.RawMessage{encoded})
	}
	content, err := json.Marshal(append([]protocol.ContentPart{}, parts...))
	if err != nil {
		return err
	}
	item := Item{Type: ItemMessage, Role: role, Content: content}
	encoded, err := json.Marshal(item)
	if err != nil {
		return err
	}
	return state.AppendRecords([]json.RawMessage{encoded})
}

func (*Context) ChangeEffort(string) error { return nil }

func (*Context) AppendExecutionState(*protocol.ExecutionState) error { return nil }

func (state *Context) AppendToolResult(call protocol.ToolInvocation, parts []protocol.ContentPart) error {
	if !call.HasValidShape() {
		return fmt.Errorf("invalid tool invocation: %w", protocol.ErrInvalid)
	}
	known, err := state.FindCall(call.CallID)
	if err != nil {
		return err
	}
	if known.Encoding != call.Encoding || known.Name != call.Name || known.Input != call.Input ||
		(call.SourceRecord != 0 && known.SourceRecord != call.SourceRecord) {
		return fmt.Errorf("tool invocation provenance changed for %q: %w", call.CallID, protocol.ErrInvalid)
	}
	outputType := ItemFunctionOutput
	if call.Encoding == protocol.ExecEncodingFreeform {
		outputType = ItemCustomToolOutput
	}
	item := Item{Type: outputType, CallID: call.CallID, Output: append([]protocol.ContentPart{}, parts...)}
	encoded, err := json.Marshal(item)
	if err != nil {
		return err
	}
	return state.AppendRecords([]json.RawMessage{encoded})
}

// FindCall returns a call by ID even after its first output. Responses allows asynchronous
// notifications to append further output records for the same call.
func (state *Context) FindCall(callID string) (protocol.ToolInvocation, error) {
	if err := validateItems(state.Items); err != nil {
		return protocol.ToolInvocation{}, err
	}
	for index, item := range state.Items {
		call, ok := item.ToolCall()
		if !ok || call.CallID != callID {
			continue
		}
		call.SourceRecord = index + 1
		return call, nil
	}
	return protocol.ToolInvocation{}, protocol.ErrMissing
}

func (state *Context) PendingCalls() ([]protocol.ToolInvocation, error) {
	for index, item := range state.Items {
		if err := validateItem(item); err != nil {
			return nil, fmt.Errorf("Responses record %d: %w", index+1, err)
		}
	}
	return pendingCalls(state.Items)
}

func (state *Context) CheckSettled() error {
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
	return &protocol.UnknownEffectError{Reason: fmt.Sprintf("unresolved tool calls %v", ids)}
}

func (state *Context) TurnComplete() bool {
	if len(state.Items) == 0 || state.CheckSettled() != nil {
		return false
	}
	last := state.Items[len(state.Items)-1]
	return last.Type == ItemMessage && last.Role == "assistant"
}

func validateItems(items []Item) error {
	for index, item := range items {
		if err := validateItem(item); err != nil {
			return fmt.Errorf("Responses record %d: %w", index+1, err)
		}
	}
	_, err := pendingCalls(items)
	return err
}

func validateItem(item Item) error {
	switch item.Type {
	case ItemMessage, ItemReasoning, ItemCustomToolCall, ItemCustomToolOutput, ItemFunctionCall,
		ItemFunctionOutput, ItemCompaction:
	default:
		return fmt.Errorf("unsupported native Responses item type %q: %w", item.Type, protocol.ErrInvalid)
	}
	if _, isCall := item.ToolCall(); isCall && !item.HasValidToolCallShape() {
		return fmt.Errorf("malformed tool call: %w", protocol.ErrInvalid)
	}
	return nil
}

// pendingCalls validates call/output links and returns unanswered calls in native record order.
// Repeated outputs are retained and accepted for asynchronous tool notifications.
func pendingCalls(items []Item) ([]protocol.ToolInvocation, error) {
	seen := make(map[string]protocol.ToolInvocation)
	completed := make(map[string]struct{})
	var calls []protocol.ToolInvocation
	for index, item := range items {
		if call, ok := item.ToolCall(); ok {
			call.SourceRecord = index + 1
			if _, exists := seen[call.CallID]; exists {
				return nil, &protocol.UnknownEffectError{Reason: "duplicate tool call " + call.CallID}
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
			return nil, &protocol.UnknownEffectError{Reason: "orphan tool output " + item.CallID}
		}
		want := ItemFunctionOutput
		if call.Encoding == protocol.ExecEncodingFreeform {
			want = ItemCustomToolOutput
		}
		if item.Type != want {
			return nil, &protocol.UnknownEffectError{Reason: fmt.Sprintf("tool output %s does not match %s call %s", item.Type, call.Encoding, item.CallID)}
		}
		completed[item.CallID] = struct{}{}
	}
	pending := make([]protocol.ToolInvocation, 0, len(calls))
	for _, call := range calls {
		if _, done := completed[call.CallID]; !done {
			pending = append(pending, call)
		}
	}
	return pending, nil
}

var _ protocol.Context = (*Context)(nil)
