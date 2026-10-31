package centralsampler

import (
	"encoding/json"

	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/anthropic"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/protocol"
	"github.com/agentydragon/ducktape/agentplane/x/central_sampler/responses"
)

func newNativeContext(api protocol.APIStyle) (protocol.Context, error) {
	switch api {
	case protocol.APIStyleResponses:
		return &responses.Context{}, nil
	case protocol.APIStyleAnthropicMessages:
		return &anthropic.Context{}, nil
	default:
		return nil, protocol.ErrInvalid
	}
}

func decodeNativeContext(api protocol.APIStyle, raw []byte) (protocol.Context, error) {
	state, err := newNativeContext(api)
	if err != nil {
		return nil, err
	}
	if err := json.Unmarshal(raw, state); err != nil {
		return nil, err
	}
	return state, nil
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

func cloneNativeContext(state protocol.Context) (protocol.Context, error) {
	wire, err := json.Marshal(state)
	if err != nil {
		return nil, err
	}
	return decodeNativeContext(state.API(), wire)
}
