package jsonwire

import (
	"bytes"
	"encoding/json"
	"errors"
	"io"

	"github.com/agentydragon/ducktape/agentplane/env/jsonrpc"
)

func DecodeJSONValue(data []byte) (any, error) {
	decoder := json.NewDecoder(bytes.NewReader(data))
	decoder.UseNumber()
	var value any
	if err := decoder.Decode(&value); err != nil {
		return nil, err
	}
	if err := decoder.Decode(new(any)); !errors.Is(err, io.EOF) {
		if err == nil {
			return nil, errors.New("JSON contains trailing data")
		}
		return nil, err
	}
	return value, nil
}

func Object(value any) (map[string]any, error) {
	data, err := json.Marshal(value)
	if err != nil {
		return nil, err
	}
	var object map[string]any
	if err := jsonrpc.UnmarshalObject(data, &object); err != nil {
		return nil, err
	}
	return object, nil
}
