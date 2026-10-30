package centralsampler

import "encoding/json"

// NativeContext is sealed to the two protocol implementations. Records are native API objects;
// execution decisions never become additional conversation records.
type NativeContext interface {
	json.Marshaler
	API() APIStyle
	Records() ([]json.RawMessage, error)
	AppendRecords([]json.RawMessage) error
	AppendUser([]ContentPart) error
	AppendNotice([]ContentPart) error
	AppendToolResult(ToolInvocation, []ContentPart) error
	ChangeEffort(string) error
	AppendExecutionState(*ExecutionState) error
	PendingCalls() ([]ToolInvocation, error)
	FindCall(string) (ToolInvocation, error)
	CheckSettled() error
	TurnComplete() bool
	nativeContext()
}

func newNativeContext(api APIStyle) (NativeContext, error) {
	switch api {
	case APIStyleResponses:
		return &ResponsesContext{}, nil
	case APIStyleAnthropicMessages:
		return &AnthropicContext{}, nil
	default:
		return nil, ErrInvalid
	}
}

func decodeNativeContext(api APIStyle, raw []byte) (NativeContext, error) {
	state, err := newNativeContext(api)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, state); err != nil {
		return nil, err
	}
	return state, nil
}

func decodeNativeRecords(api APIStyle, records []json.RawMessage) (NativeContext, error) {
	state, err := newNativeContext(api)
	if err != nil {
		return nil, err
	}
	if err := state.AppendRecords(records); err != nil {
		return nil, err
	}
	return state, nil
}

func cloneNativeContext(state NativeContext) (NativeContext, error) {
	wire, err := json.Marshal(state)
	if err != nil {
		return nil, err
	}
	return decodeNativeContext(state.API(), wire)
}
